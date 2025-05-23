import sys
from typing import Callable, Union, IO, Any

import six
from pathlib2 import Path

from ..frameworks import WeightsFileHandler, _Empty, _patched_call
from ..frameworks.base_bind import PatchBaseModelIO
from ..import_bind import PostImportHookPatching
from ...model import Framework


class PatchCatBoostModelIO(PatchBaseModelIO):
    _current_task = None
    __patched = None
    __callback_cls = None

    @staticmethod
    def update_current_task(task: Framework, **kwargs: Any) -> None:
        PatchCatBoostModelIO._current_task = task
        if not task:
            return
        PatchCatBoostModelIO._patch_model_io()
        PostImportHookPatching.add_on_import("catboost", PatchCatBoostModelIO._patch_model_io)

    @staticmethod
    def _patch_model_io() -> None:
        if PatchCatBoostModelIO.__patched:
            return
        if "catboost" not in sys.modules:
            return
        PatchCatBoostModelIO.__patched = True
        # noinspection PyBroadException
        try:
            from catboost import (
                CatBoost,
                CatBoostClassifier,
                CatBoostRegressor,
                CatBoostRanker,
            )

            CatBoost.save_model = _patched_call(CatBoost.save_model, PatchCatBoostModelIO._save)
            CatBoost.load_model = _patched_call(CatBoost.load_model, PatchCatBoostModelIO._load)
            PatchCatBoostModelIO.__callback_cls = PatchCatBoostModelIO._generate_training_callback_class()
            CatBoost.fit = _patched_call(CatBoost.fit, PatchCatBoostModelIO._fit)
            CatBoostClassifier.fit = _patched_call(CatBoostClassifier.fit, PatchCatBoostModelIO._fit)
            CatBoostRegressor.fit = _patched_call(CatBoostRegressor.fit, PatchCatBoostModelIO._fit)
            CatBoostRanker.fit = _patched_call(CatBoostRanker.fit, PatchCatBoostModelIO._fit)
        except Exception as e:
            logger = PatchCatBoostModelIO._current_task.get_logger()
            logger.report_text("Failed patching Catboost. Exception is: '" + str(e) + "'")

    @staticmethod
    def _save(
        original_fn: Callable,
        obj: Any,
        f: Union[str, IO],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # see https://catboost.ai/en/docs/concepts/python-reference_catboost_save_model
        ret = original_fn(obj, f, *args, **kwargs)
        if not PatchCatBoostModelIO._current_task:
            return ret
        if isinstance(f, six.string_types):
            filename = f
        else:
            filename = None
        # give the model a descriptive name based on the file name
        # noinspection PyBroadException
        try:
            model_name = Path(filename).stem
        except Exception:
            model_name = None
        WeightsFileHandler.create_output_model(
            obj,
            filename,
            Framework.catboost,
            PatchCatBoostModelIO._current_task,
            singlefile=True,
            model_name=model_name,
        )
        return ret

    @staticmethod
    def _load(original_fn: Callable, f: Union[str, Any], *args: Any, **kwargs: Any) -> Any:
        # see https://catboost.ai/en/docs/concepts/python-reference_catboost_load_model
        if not PatchCatBoostModelIO._current_task:
            return original_fn(f, *args, **kwargs)

        if isinstance(f, six.string_types):
            filename = f
        elif len(args) >= 1 and isinstance(args[0], six.string_types):
            filename = args[0]
        else:
            filename = None

        # register input model
        empty = _Empty()
        model = original_fn(f, *args, **kwargs)
        WeightsFileHandler.restore_weights_file(empty, filename, Framework.catboost, PatchCatBoostModelIO._current_task)
        if empty.trains_in_model:
            # noinspection PyBroadException
            try:
                model.trains_in_model = empty.trains_in_model
            except Exception:
                pass
        return model

    @staticmethod
    def _fit(original_fn: Callable, obj: Any, *args: Any, **kwargs: Any) -> Any:
        if not PatchCatBoostModelIO._current_task:
            return original_fn(obj, *args, **kwargs)
        callbacks = kwargs.get("callbacks") or []
        kwargs["callbacks"] = callbacks + [PatchCatBoostModelIO.__callback_cls(task=PatchCatBoostModelIO._current_task)]
        # noinspection PyBroadException
        try:
            return original_fn(obj, *args, **kwargs)
        except Exception:
            logger = PatchCatBoostModelIO._current_task.get_logger()
            logger.report_text(
                "Catboost metrics logging is not supported for GPU. "
                "See https://github.com/catboost/catboost/issues/1792"
            )
            del kwargs["callbacks"]
            return original_fn(obj, *args, **kwargs)

    @staticmethod
    def _generate_training_callback_class() -> Any:
        class ClearMLCallback:
            _scalar_index_counter = 0

            def __init__(self, task: Any) -> None:
                self._logger = task.get_logger()
                self._scalar_index = ClearMLCallback._scalar_index_counter
                ClearMLCallback._scalar_index_counter += 1

            def after_iteration(self, info: Any) -> bool:
                info = vars(info)
                iteration = info.get("iteration")
                for title, metric in (info.get("metrics") or {}).items():
                    if self._scalar_index != 0:
                        title = "{} - {}".format(title, self._scalar_index)
                    for series, log in metric.items():
                        value = log[-1]
                        self._logger.report_scalar(title=title, series=series, value=value, iteration=iteration)
                return True

        return ClearMLCallback
