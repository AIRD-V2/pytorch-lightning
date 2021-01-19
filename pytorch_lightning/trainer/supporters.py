# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
from torch import Tensor
from torch.utils.data import Dataset, IterableDataset
from torch.utils.data.dataloader import DataLoader

from pytorch_lightning.trainer.connectors.logger_connector.logger_connector import LoggerStages
from pytorch_lightning.utilities import rank_zero_warn
from pytorch_lightning.utilities.apply_func import apply_to_collection
from pytorch_lightning.utilities.cloud_io import get_filesystem
from pytorch_lightning.utilities.data import get_len
from pytorch_lightning.utilities.exceptions import MisconfigurationException


class TensorRunningAccum(object):
    """Tracks a running accumulation values (min, max, mean) without graph
    references.

    Examples:
        >>> accum = TensorRunningAccum(5)
        >>> accum.last(), accum.mean()
        (None, None)
        >>> accum.append(torch.tensor(1.5))
        >>> accum.last(), accum.mean()
        (tensor(1.5000), tensor(1.5000))
        >>> accum.append(torch.tensor(2.5))
        >>> accum.last(), accum.mean()
        (tensor(2.5000), tensor(2.))
        >>> accum.reset()
        >>> _= [accum.append(torch.tensor(i)) for i in range(13)]
        >>> accum.last(), accum.mean(), accum.min(), accum.max()
        (tensor(12.), tensor(10.), tensor(8.), tensor(12.))
    """

    def __init__(self, window_length: int):
        self.window_length = window_length
        self.memory = None
        self.current_idx: int = 0
        self.last_idx: Optional[int] = None
        self.rotated: bool = False

    def reset(self) -> None:
        """Empty the accumulator."""
        self.__init__(self.window_length)

    def last(self):
        """Get the last added element."""
        if self.last_idx is not None:
            return self.memory[self.last_idx]

    def append(self, x):
        """Add an element to the accumulator."""
        if self.memory is None:
            self.memory = torch.zeros(self.window_length, *x.shape)

        # ensure same device and type
        if self.memory.device != x.device or self.memory.type() != x.type():
            x = x.to(self.memory)

        # store without grads
        with torch.no_grad():
            self.memory[self.current_idx] = x
            self.last_idx = self.current_idx

        # increase index
        self.current_idx += 1

        # reset index when hit limit of tensor
        self.current_idx = self.current_idx % self.window_length
        if self.current_idx == 0:
            self.rotated = True

    def mean(self):
        """Get mean value from stored elements."""
        return self._agg_memory('mean')

    def max(self):
        """Get maximal value from stored elements."""
        return self._agg_memory('max')

    def min(self):
        """Get minimal value from stored elements."""
        return self._agg_memory('min')

    def _agg_memory(self, how: str):
        if self.last_idx is not None:
            if self.rotated:
                return getattr(self.memory, how)()
            else:
                return getattr(self.memory[: self.current_idx], how)()


class Accumulator(object):
    def __init__(self):
        self.num_values = 0
        self.total = 0

    def accumulate(self, x):
        with torch.no_grad():
            self.total += x
            self.num_values += 1

    def mean(self):
        return self.total / self.num_values


class LightningBatchSamplerWrapper:
    """
    This class wraps user batch sampler, so we can extract
    the batch_indices for tracking each sample.
    """

    SKIP_KEYS = ['sampler', 'batch_sampler', 'dataset_kind']
    SKIP_VALID_KEYS = ['args', 'kwargs', 'self']

    def __init__(self, batch_sampler):
        self.batch_sampler = batch_sampler
        self.batch_indices = None

    def __iter__(self):
        for batch_indices in self.batch_sampler:
            self.batch_indices = batch_indices
            yield batch_indices

    @staticmethod
    def recreate_dataloader(dataloader: DataLoader) -> DataLoader:
        """
        This function will wrap the user batch_sampler to track the returned batch indices
        """
        if not isinstance(dataloader, DataLoader):
            raise MisconfigurationException('Autoid only works with torch dataloaders or derived classes!')

        elif isinstance(dataloader.dataset, IterableDataset):
            return dataloader

        params = {k: v for k, v in vars(dataloader).items() if not k.startswith("_")}

        valid_kwargs = set(inspect.signature(dataloader.__init__).parameters)
        contains_dataset = True

        if type(dataloader) is not DataLoader:
            contains_dataset = "dataset" in valid_kwargs
            valid_kwargs.update(inspect.signature(DataLoader.__init__).parameters)

        dl_args = {
            name: params[name] for name in valid_kwargs
            if name in params and name not in LightningBatchSamplerWrapper.SKIP_KEYS
        }

        multiprocessing_context = dataloader.multiprocessing_context

        # override parameters to enable batch_sampler injection
        dl_args.update({
            "batch_size": 1,
            "sampler": None,
            "shuffle": None,
            "batch_sampler": LightningBatchSamplerWrapper(dataloader.batch_sampler),
            "drop_last": False,
            "multiprocessing_context": multiprocessing_context,
        })

        missing_kwargs = valid_kwargs.difference(LightningBatchSamplerWrapper.SKIP_VALID_KEYS).difference(dl_args)
        if missing_kwargs:
            dataloader_cls_name = dataloader.__class__.__name__
            rank_zero_warn(
                f"Trying to replace to wrap your BatchSampler in your {dataloader_cls_name} dataloader."
                "This would fail as your DataLoader doesn't expose as attributes all its __init__ parameters. "
                f"Missing attributes are {missing_kwargs} \n"
                "HINT: use Trainer(enable_predict_auto_id=False) and provide your own id."
                " Check out the doc for Testing.", UserWarning
            )
            return dataloader

        if not contains_dataset:
            del dl_args['dataset']

        # re-create object of the same class with new argumnets
        dataloader = type(dataloader)(**dl_args)
        dataloader.multiprocessing_context = multiprocessing_context
        return dataloader


class PredictionCollection(object):

    """
    This class is used to collect predictions.

    The legacy API built using the following functions:
        - LightningModule.write_predictions: Entry point for user

        - add
        - _add_prediction
        - to_disk

        This should be used when the test dataset predictions are too large,
        and each rank will save the predictions

    The new API built using the following functions:
        - LightningModule.add_predictions: Entry point for user

        - append: Receive a new predictions to append to cache. Handle validation
        - _append_prediction: Handle storing
        - finalize_predictions: When the epoch is finished, add predictions to results object
        - all_gather_predictions: Gather predictions accross multiple processses.

        This should be used with medium sized dataset,
        where all predictions can be hold in memory.

        On each test_step, if the user call add_predictions, append and _append_prediction
            will be used to append predictions to internal cache.
        On the end of `trainer.test`, predictions will be added to result object using
            `finalize_predictions` function.
    """

    ID_KEY = 'id'

    def __init__(self, global_rank: int, world_size: int, all_gather_fn: Callable):
        self.global_rank = global_rank
        self.world_size = world_size
        self.all_gather_fn = all_gather_fn
        self._legacy_predictions = {}
        self._predictions = {stage: {} for stage in LoggerStages}

    @property
    def predictions(self):
        return self._predictions[self.current_stage]

    def _append_prediction(
        self,
        predictions: Union[List, Tuple, torch.Tensor],
        dl_idx: int,
        batch_indices: List[int],
        enable_predict_auto_id: bool
    ) -> None:

        cache = self.predictions

        cache.setdefault(dl_idx, {})

        def _convert(value):
            return value.cpu().tolist()

        for batch_idx, pred in enumerate(predictions):
            if enable_predict_auto_id:
                sample_id = batch_indices[batch_idx]

            else:
                if self.ID_KEY in pred:
                    sample_id = pred[self.ID_KEY]
                    if not isinstance(sample_id, (int, float)):
                        raise MisconfigurationException(
                            f"`id` key should be either a int or float. Found {type(sample_id)}."
                        )
                else:
                    raise MisconfigurationException(
                        f"The predictions dict requires an `{self.ID_KEY}` key."
                    )

            if sample_id in cache[dl_idx]:
                raise MisconfigurationException(
                    "Prediction Collection doesn't support multiple predictions for one sample yet.")

            if enable_predict_auto_id:
                cache[dl_idx][sample_id] = {
                    self.ID_KEY: sample_id, "predictions": pred}
            else:
                cache[dl_idx][sample_id] = pred

            # apply convert to store memory
            cache[dl_idx][sample_id] = apply_to_collection(cache[dl_idx][sample_id], torch.Tensor, _convert)

    def append(
        self,
        predictions: Union[List, Tuple, torch.Tensor],
        dl_idx: int,
        batch_indices: List[int],
        current_stage: str,
        enable_predict_auto_id: bool
    ) -> None:
        """
        This function expects predictions to be a list of tensors or dictionary of tensors.
        Example::

            self.add_predictions(predictions)
        """
        if predictions is None or (isinstance(predictions, list) and not predictions):
            return

        self.current_stage = current_stage

        if enable_predict_auto_id:
            assert isinstance(predictions, (list, tuple, torch.Tensor))
            if batch_indices is None:
                return

            if len(predictions) != len(batch_indices):
                raise MisconfigurationException(
                    "The predictions dimension should match the batch_size. "
                    "HINT: If your prediction dimensions don't match the size of your batch size, "
                    "use Trainer(enable_predict_auto_id=False) and provide your own `id`."
                )
        else:
            if not all(isinstance(p, dict) and "id" in p for p in predictions):
                raise MisconfigurationException(
                    "predictions objects should be a list where each element is a dict. "
                    "each dict should contain an unique number `id` to identify each sample."
                )

            if not all(len(p) > 1 for p in predictions):
                raise MisconfigurationException(
                    "each element should contain at least an unique number `id` and a prediction tensor."
                )

        self._append_prediction(predictions, dl_idx, batch_indices, enable_predict_auto_id)

    def should_finalize_predictions(self, current_stage):
        self.current_stage = current_stage
        return len(self.predictions) > 0

    def finalize_predictions(self, results: List[Dict]) -> List[Dict]:
        """
        This function will add the reduced predictions accross multiple processes for each dataset
        to the results objects returned by the trainer.test function.
        """
        predictions = self.predictions
        for dl_idx, result in enumerate(results):
            if dl_idx in predictions:
                dl_predictions = predictions[dl_idx]
                dl_predictions = self.all_gather_predictions(dl_predictions)
                result["predictions"] = list(dl_predictions.values())
        return results

    def all_gather_predictions(self, predictions: Dict) -> Dict:
        """
        This function all_gather predictions accross multiple processes
        # todo: see https://github.com/PyTorchLightning/pytorch-lightning/issues/5493 for better details.
        """
        if not (self.world_size >= 2 and torch.distributed.is_available() and torch.distributed.is_initialized()):
            return predictions

        predictions = self.all_gather_fn(predictions)

        def gather(pred: Union[List[Tensor], Tensor], idx: int) -> Union[List[Tensor], Tensor]:
            # all_gather: tensor [(N, C), ..., (N, C)] -> [(WORLD_SIZE, N, C), ..., (WORLD_SIZE, N, C)]
            # `convert` function is used to get the right tensor
            # depending the data id.
            def convert(p):
                return p[idx % self.world_size].tolist()
            return (
                [convert(p) for p in pred]
                if isinstance(pred, (list)) else
                convert(pred)
            )

        out = {}
        for pred in predictions.values():
            keys = [k for k in pred if k != self.ID_KEY]
            ids = pred[self.ID_KEY].int().tolist()
            for id_ in ids:
                if id_ not in out:
                    res = {k: apply_to_collection(pred[k], (torch.Tensor, list), gather, id_) for k in keys}
                    res[self.ID_KEY] = id_
                    out[id_] = res
        return out

    def _add_prediction(self, name, values, filename):
        if filename not in self._legacy_predictions:
            self._legacy_predictions[filename] = {name: values}
        elif name not in self._legacy_predictions[filename]:
            self._legacy_predictions[filename][name] = values
        elif isinstance(values, Tensor):
            self._legacy_predictions[filename][name] = torch.cat(
                (self._legacy_predictions[filename][name], values)
            )
        elif isinstance(values, list):
            self._legacy_predictions[filename][name].extend(values)

    def add(self, predictions):
        if predictions is None:
            return

        for filename, pred_dict in predictions.items():
            for feature_name, values in pred_dict.items():
                self._add_prediction(feature_name, values, filename)

    def to_disk(self) -> None:
        """Write predictions to file(s).
        """
        for filepath, predictions in self._legacy_predictions.items():
            fs = get_filesystem(filepath)
            # normalize local filepaths only
            if fs.protocol == "file":
                filepath = os.path.realpath(filepath)
            if self.world_size > 1:
                stem, extension = os.path.splitext(filepath)
                filepath = f"{stem}_rank_{self.global_rank}{extension}"
            dirpath = os.path.split(filepath)[0]
            fs.mkdirs(dirpath, exist_ok=True)

            # Convert any tensor values to list
            predictions = {
                k: v if not isinstance(v, Tensor) else v.tolist()
                for k, v in predictions.items()
            }

            # Check if all features for this file add up to same length
            feature_lens = {k: len(v) for k, v in predictions.items()}
            if len(set(feature_lens.values())) != 1:
                raise ValueError(
                    "Mismatching feature column lengths found in stored EvalResult predictions."
                )

            # Switch predictions so each entry has its own dict
            outputs = []
            for values in zip(*predictions.values()):
                output_element = {k: v for k, v in zip(predictions.keys(), values)}
                outputs.append(output_element)

            # Write predictions for current file to disk
            with fs.open(filepath, "wb") as fp:
                torch.save(outputs, fp)


class CycleIterator(object):
    """
    Iterator for restarting a dataloader if it runs out of samples
    """
    def __init__(self, loader: Any, length: Optional[int] = None):
        """

        Args:
            loader: the loader to restart for cyclic (and optionally infinite) sampling
            length: the number of batches to sample (with restarted loaders if necessary) before raising StopIteration
                if None: infinite

        """
        if length is None:
            length = float('inf')

        self.length = length
        self.loader = loader
        self._loader_iter = None
        self.counter = 0

    def __iter__(self) -> Any:
        """

        Creates the internal iterator and returns self

        Returns:
            CycleIterator: self

        """
        self.counter = 0
        self._loader_iter = iter(self.loader)
        return self

    def __next__(self) -> Any:
        """
        Fetches the next batch from internal dataloader and restarts
        it if necessary

        Returns:
            Any: the resulting batch

        Raises:
            StopIteration: if more then :attr:`length` batches have been returned

        """
        # Note: if self.length is `inf`, then the iterator will never stop
        if self.counter >= self.__len__():
            raise StopIteration

        try:
            return next(self._loader_iter)

        except StopIteration:
            self._loader_iter = iter(self.loader)
            return next(self._loader_iter)

        finally:
            self.counter += 1

    def __len__(self) -> Union[int, float]:
        return self.length


class CombinedDataset(object):
    """
    Combine multiple datasets and compute their statistics
    """
    COMPUTE_FUNCS = {'min_size': min, 'max_size_cycle': max}

    def __init__(self, datasets: Union[Sequence, Mapping], mode: str = 'min_size'):
        """

        Args:
            datasets: a sequence/mapping datasets. Can be a collections of torch.utils.Dataset,
                Iterable or even None.
            mode: whether to use the minimum number of batches in all samples or the maximum
                number of batches in all samples.

        """
        self.datasets = datasets
        if mode not in self.COMPUTE_FUNCS.keys():
            raise MisconfigurationException(
                f'You have selected unsupported mode "{mode}",'
                f' please select one the: {list(self.COMPUTE_FUNCS.keys())}.'
            )
        self.mode = mode

    @property
    def max_len(self) -> Union[int, float]:
        return self._calc_num_data(self.datasets, 'max_size_cycle')

    @property
    def min_len(self) -> Union[int, float]:
        return self._calc_num_data(self.datasets, 'min_size')

    @staticmethod
    def _calc_num_data(datasets: Union[Sequence, Mapping], mode: str) -> Union[int, float]:
        """
        Compute the length of `CombinedDataset` according to the `mode`.

        Args:
            datasets: a sequence/mapping datasets. Can be a collections of torch.utils.data.Dataset,
                Iterable or even None.
            mode: Determine `CombinedDataset`'s length is the maximum or minimum of
                the datasets.

        Returns:
            length: the length of `CombinedDataset`

        """
        if mode not in CombinedDataset.COMPUTE_FUNCS.keys():
            raise MisconfigurationException(f"Invalid Mode: {mode}")

        # extract the lengths
        all_lengths = apply_to_collection(datasets, (Dataset, Iterable, type(None)), get_len,
                                          wrong_dtype=(Sequence, Mapping))

        compute_func = CombinedDataset.COMPUTE_FUNCS[mode]

        if isinstance(all_lengths, (int, float)):
            length = all_lengths

        elif isinstance(all_lengths, Mapping):
            length = compute_func(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            length = compute_func(all_lengths)

        return length

    def __len__(self) -> int:
        """Return the minimum length of the datasets."""
        return self._calc_num_data(self.datasets, self.mode)


class CombinedLoader(object):
    """
    Combines different dataloaders and allows sampling in parallel.

    Supported modes are 'min_size', which raises StopIteration after the shortest loader
    (the one with the lowest number of batches) is done, and 'max_size_cycle` which raises
    StopIteration after the longest loader (the one with most batches) is done, while cycling
    through the shorter loaders.

    Examples:
        >>> loaders = {'a': torch.utils.data.DataLoader(range(6), batch_size=4),
        ...            'b': torch.utils.data.DataLoader(range(15), batch_size=5)}
        >>> combined_loader = CombinedLoader(loaders, 'max_size_cycle')
        >>> for item in combined_loader:
        ...     print(item)
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([0, 1, 2, 3, 4])}
        {'a': tensor([4, 5]), 'b': tensor([5, 6, 7, 8, 9])}
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([10, 11, 12, 13, 14])}
        >>> combined_loader = CombinedLoader(loaders, 'min_size')
        >>> for item in combined_loader:
        ...     print(item)
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([0, 1, 2, 3, 4])}
        {'a': tensor([4, 5]), 'b': tensor([5, 6, 7, 8, 9])}

    """
    SUPPORTED_MODES = ('min_size', 'max_size_cycle')

    def __init__(self, loaders: Any, mode: str = 'min_size'):
        """

        Args:
            loaders: the loaders to sample from. Can be all kind of collection
            mode: the mode. Supported are 'min_size' which stops if the shortest loader is exhausted and
                'max_size_cycle' which stops if the longest loader is exhausted and cycles through the smaller ones.

        """
        self.loaders = loaders

        datasets = apply_to_collection(self.loaders, Iterable, getattr, 'dataset', None,
                                       wrong_dtype=(Sequence, Mapping))
        # could be multiple datasets, but use self.dataset to follow the name convention in DataLoader
        self.dataset = CombinedDataset(datasets, mode)

        if mode not in self.SUPPORTED_MODES:
            raise MisconfigurationException(f"Invalid Mode: {mode}")

        self.mode = mode

        if self.mode == 'max_size_cycle':
            self._wrap_loaders_max_size_cycle()

    @property
    def sampler(self) -> Union[Iterable, Sequence, Mapping]:
        """Return a collections of samplers extracting from loaders."""
        return apply_to_collection(self.loaders, Iterable, getattr, 'sampler', None,
                                   wrong_dtype=(Sequence, Mapping))

    def _wrap_loaders_max_size_cycle(self) -> Any:
        """
        Wraps all loaders to make sure they are cycled until the longest loader is exhausted

        Returns:
            Any: the wrapped loaders

        """
        all_lengths = apply_to_collection(self.loaders, Iterable, get_len,
                                          wrong_dtype=(Sequence, Mapping))

        if isinstance(all_lengths, (int, float)):
            length = all_lengths

        elif isinstance(all_lengths, Mapping):
            length = max(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            length = max(all_lengths)

        if isinstance(self.loaders, Mapping):
            self.loaders = type(self.loaders)({k: CycleIterator(v, length=length)
                                               for k, v in self.loaders.items()})

        elif isinstance(self.loaders, Sequence):
            self.loaders = type(self.loaders)([
                CycleIterator(v, length=length) for v in self.loaders
            ])

        # dataloaders are iterable but not sequence
        elif isinstance(self.loaders, Iterable):
            # only one dataloader, just keep it the same.
            pass
        else:
            raise ValueError(f'Invalid Datatype for loaders: {type(self.loaders).__name__}')

    def __iter__(self) -> Any:
        """
        Create and return an iterator, `CombinedLoaderIterator`, for the combined loader.
        """
        return CombinedLoaderIterator(self.loaders)

    @staticmethod
    def _calc_num_batches(loaders: Any) -> Union[int, float]:
        """
        Compute the length (aka the number of batches) of `CombinedLoader`.

        Args:
            loaders: a collections of loaders.

        Returns:
            length: the minimum length of loaders

        """
        all_lengths = apply_to_collection(loaders, Iterable, get_len,
                                          wrong_dtype=(Sequence, Mapping))

        if isinstance(all_lengths, (int, float)):
            return all_lengths

        elif isinstance(all_lengths, Mapping):
            return min(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            return min(all_lengths)

        raise TypeError(f'Got Type {type(all_lengths).__name__}, but expected one of Sequence, int or Mapping')

    def __len__(self) -> int:
        return self._calc_num_batches(self.loaders)


class CombinedLoaderIterator(object):
    """
    Custom Iterator returning data from multple loaders, and allows sampling in parallel
    """
    def __init__(self, loaders: Any):
        """

        Args:
            loaders: the loaders to sample from. Can be all kind of collection

        """
        self.loaders = loaders
        self._loader_iters = None

    @property
    def loader_iters(self) -> Any:
        """
        Get the `_loader_iters` and create one if it is None.
        """
        if self._loader_iters is None:
            self._loader_iters = self.create_loader_iters(self.loaders)

        return self._loader_iters

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        """
        Fetches the next batch from multiple data loaders

        Returns:
            Any: a collections of batch data

        """
        return self.request_next_batch(self.loader_iters)

    @staticmethod
    def request_next_batch(loader_iters: Union[Iterator, Sequence, Mapping]) -> Any:
        """
        Return the batch of data from multiple iterators.

        Args:
            loader_iters: a collections of iterators

        Returns
            Any: a collections of batch data

        """
        return apply_to_collection(loader_iters, Iterator, next)

    @staticmethod
    def create_loader_iters(
        loaders: Union[Any, Iterator, Sequence, Mapping]
    ) -> Union[Any, Iterator, Sequence, Mapping]:
        """
        Create and return a collection of iterators from loaders.

        Args:
            loaders: a collections of loaders

        Returns
            a collections of iterators

        """
        # dataloaders are Iterable but not Sequences. Need this to specifically exclude sequences
        return apply_to_collection(loaders, Iterable, iter, wrong_dtype=(Sequence, Mapping))
