import os
import shutil
import sys
import threading
from enum import Enum
from random import randint
from tempfile import mkstemp
from typing import TYPE_CHECKING, Callable, Dict, Optional, Union, Any

import six
from pathlib2 import Path

from ...backend_interface.model import Model
from ...config import running_remotely
from ...debugging.log import get_logger
from ...model import InputModel, OutputModel

if TYPE_CHECKING:
    from ...task import Task

TrainsFrameworkAdapter = "frameworks"
_recursion_guard = {}


def _patched_call(original_fn: Callable, patched_fn: Callable) -> Callable:
    def _inner_patch(*args: Any, **kwargs: Any) -> Any:
        # noinspection PyProtectedMember,PyUnresolvedReferences
        ident = threading._get_ident() if six.PY2 else threading.get_ident()
        if ident in _recursion_guard:
            return original_fn(*args, **kwargs)
        _recursion_guard[ident] = 1
        ret = None
        try:
            ret = patched_fn(original_fn, *args, **kwargs)
        except Exception as ex:
            raise ex
        finally:
            try:
                _recursion_guard.pop(ident)
            except KeyError:
                pass
        return ret

    return _inner_patch


def _patched_call_no_recursion_guard(original_fn: Callable, patched_fn: Callable) -> Callable:
    def _inner_patch(*args: Any, **kwargs: Any) -> Any:
        return patched_fn(original_fn, *args, **kwargs)

    return _inner_patch


class _Empty(object):
    def __init__(self) -> None:
        self.trains_in_model = None


class WeightsFileHandler(object):
    # _model_out_store_lookup = {}
    # _model_in_store_lookup = {}
    _model_store_lookup_lock = threading.Lock()
    _model_pre_callbacks = {}
    _model_post_callbacks = {}
    model_wildcards = {}

    class CallbackType(Enum):
        def __str__(self) -> str:
            return str(self.value)

        def __eq__(self, other: Any) -> bool:
            return str(self) == str(other)

        save = "save"
        load = "load"

    class ModelInfo(object):
        def __init__(
            self,
            model: Optional[Model],
            upload_filename: Optional[str],
            local_model_path: str,
            local_model_id: str,
            framework: str,
            task: "Task",
        ) -> None:
            """
            :param model: None, OutputModel or InputModel
            :param upload_filename: example 'filename.ext'
            :param local_model_path: example /local/copy/filename.random_number.ext'
            :param local_model_id: example /local/copy/filename.ext'
            :param framework: example 'PyTorch'
            :param task: Task object
            """
            self.model = model
            self.upload_filename = upload_filename
            self.local_model_path = local_model_path
            self.local_model_id = local_model_id
            self.framework = framework
            self.task = task
            # temporary store reference to the actual model/weights object that was saved.
            # only valid for store callbacks
            self.weights_object = None

    @staticmethod
    def _add_callback(func: Callable, target: Dict[int, Callable]) -> int:
        if func in target.values():
            return [k for k, v in target.items() if v == func][0]

        while True:
            h = randint(0, 1 << 31)
            if h not in target:
                break

        target[h] = func
        return h

    @staticmethod
    def _remove_callback(handle: int, target: Dict[int, Callable]) -> bool:
        if handle in target:
            target.pop(handle, None)
            return True
        return False

    @classmethod
    def add_pre_callback(
        cls,
        callback_function: Callable[
            [
                Union[str, "WeightsFileHandler.CallbackType"],
                "WeightsFileHandler.ModelInfo",
            ],
            Optional["WeightsFileHandler.ModelInfo"],
        ],
    ) -> int:
        # noqa
        """
        Add a pre-save/load callback for weights files and return its handle. If the callback was already added,
         return the existing handle.

        Use this callback to modify the weights filename registered in the ClearML Server. In case ClearML is
         configured to upload the weights file, this will affect the uploaded filename as well.
         Callback returning None will disable the tracking of the current call Model save,
         it will not disable saving it to disk, just the logging/tracking/uploading.

        :param callback_function: A function accepting action type ("load" or "save"),
            callback_function('load' or 'save', WeightsFileHandler.ModelInfo) -> WeightsFileHandler.ModelInfo
        :return Callback handle
        """
        return cls._add_callback(callback_function, cls._model_pre_callbacks)

    @classmethod
    def add_post_callback(
        cls,
        callback_function: Callable[
            [
                Union[str, "WeightsFileHandler.CallbackType"],
                "WeightsFileHandler.ModelInfo",
            ],
            "WeightsFileHandler.ModelInfo",
        ],
    ) -> int:
        # noqa
        """
        Add a post-save/load callback for weights files and return its handle.
        If the callback was already added, return the existing handle.

        :param callback_function: A function accepting action type ("load" or "save"),
            callback_function('load' or 'save', WeightsFileHandler.ModelInfo) -> WeightsFileHandler.ModelInfo
        :return Callback handle
        """
        return cls._add_callback(callback_function, cls._model_post_callbacks)

    @classmethod
    def remove_pre_callback(cls, handle: int) -> bool:
        """
        Add a pre-save/load callback for weights files and return its handle.
        If the callback was already added, return the existing handle.

        :param handle: A callback handle returned from :meth:WeightsFileHandler.add_pre_callback
        :return True if callback removed, False otherwise
        """
        return cls._remove_callback(handle, cls._model_pre_callbacks)

    @classmethod
    def remove_post_callback(cls, handle: int) -> bool:
        """
        Add a pre-save/load callback for weights files and return its handle.
        If the callback was already added, return the existing handle.

        :param handle: A callback handle returned from :meth:WeightsFileHandler.add_post_callback
        :return: True if callback removed, False otherwise
        """
        return cls._remove_callback(handle, cls._model_post_callbacks)

    @staticmethod
    def restore_weights_file(
        model: Optional[Any],
        filepath: Optional[str],
        framework: Optional[str],
        task: Optional["Task"],
    ) -> str:
        if task is None:
            return filepath

        try:
            local_model_path = os.path.abspath(filepath) if filepath else filepath
        except TypeError:
            # not a recognized type, we just return it back
            return filepath

        model_info = WeightsFileHandler.ModelInfo(
            model=None,
            upload_filename=None,
            local_model_path=local_model_path,
            local_model_id=filepath,
            framework=framework,
            task=task,
        )
        # call pre model callback functions
        for cb in list(WeightsFileHandler._model_pre_callbacks.values()):
            # noinspection PyBroadException
            try:
                model_info = cb(WeightsFileHandler.CallbackType.load, model_info)
            except Exception:
                pass

        # if callback forced us to leave they return None
        if model_info is None:
            # callback forced quit
            return filepath

        if not model_info.local_model_path:
            # get_logger(TrainsFrameworkAdapter).debug("Could not retrieve model file location, model is not logged")
            return filepath

        try:
            WeightsFileHandler._model_store_lookup_lock.acquire()

            # check if object already has InputModel
            if model_info.model:
                trains_in_model = model_info.model
            else:
                # # disable model reuse, let Model module try to find it for use
                trains_in_model, ref_model = None, None  # noqa: F841
                # trains_in_model, ref_model = WeightsFileHandler._model_in_store_lookup.get(
                #     id(model) if model is not None else None, (None, None))
                # # noinspection PyCallingNonCallable
                # if ref_model is not None and model != ref_model():
                #     # old id pop it - it was probably reused because the object is dead
                #     WeightsFileHandler._model_in_store_lookup.pop(id(model))
                #     trains_in_model, ref_model = None, None

            # check if object already has InputModel
            model_name_id = getattr(model, "name", "") if model else ""
            # noinspection PyBroadException
            try:
                config_text = None
                config_dict = trains_in_model.config_dict if trains_in_model else None
            except Exception:
                config_dict = None
                # noinspection PyBroadException
                try:
                    config_text = trains_in_model.config_text if trains_in_model else None
                except Exception:
                    config_text = None

            if not trains_in_model:
                # check if we already have the model object:
                # noinspection PyProtectedMember
                model_id, model_uri = Model._local_model_to_id_uri.get(
                    model_info.local_model_id or model_info.local_model_path,
                    (None, None),
                )
                if model_id:
                    # noinspection PyBroadException
                    try:
                        trains_in_model = InputModel(model_id)
                    except Exception:
                        model_id = None

                # if we do not, we need to import the model
                if not model_id:
                    trains_in_model = InputModel.import_model(
                        weights_url=model_info.local_model_path,
                        config_dict=config_dict,
                        config_text=config_text,
                        name=task.name + (" " + model_name_id) if model_name_id else "",
                        label_enumeration=task.get_labels_enumeration(),
                        framework=framework,
                        create_as_published=False,
                    )

            model_info.model = trains_in_model
            # call post model callback functions
            for cb in list(WeightsFileHandler._model_post_callbacks.values()):
                # noinspection PyBroadException
                try:
                    model_info = cb(WeightsFileHandler.CallbackType.load, model_info)
                except Exception:
                    pass
            trains_in_model = model_info.model

            # # disable model reuse, let Model module try to find it for use
            # if model is not None:
            #     # noinspection PyBroadException
            #     try:
            #         ref_model = weakref.ref(model)
            #     except Exception:
            #         ref_model = None
            #     WeightsFileHandler._model_in_store_lookup[id(model)] = (trains_in_model, ref_model)

            task.connect(trains_in_model)
            # if we are running remotely we should deserialize the object
            # because someone might have changed the config_dict
            # Hack: disabled
            if False and running_remotely():
                # reload the model
                model_config = trains_in_model.config_dict
                # verify that this is the same model so we are not deserializing a different model
                if (
                    config_dict
                    and config_dict.get("config")
                    and model_config
                    and model_config.get("config")
                    and config_dict.get("config").get("name") == model_config.get("config").get("name")
                ) or (not config_dict and not model_config):
                    filepath = trains_in_model.get_weights()
                    # update filepath to point to downloaded weights file
                    # actual model weights loading will be done outside the try/exception block

            # update back the internal Model lookup, and replace the local file with our file
            # noinspection PyProtectedMember
            Model._local_model_to_id_uri[model_info.local_model_id] = (
                trains_in_model.id,
                trains_in_model.url,
            )

        except Exception as ex:
            get_logger(TrainsFrameworkAdapter).debug(str(ex))
        finally:
            WeightsFileHandler._model_store_lookup_lock.release()

        return filepath

    @staticmethod
    def create_output_model(
        model: Optional[Any],
        saved_path: Optional[str],
        framework: Optional[str],
        task: Optional["Task"],
        singlefile: bool = False,
        model_name: Optional[str] = None,
        config_obj: Optional[Union[str, dict]] = None,
    ) -> str:
        if task is None:
            return saved_path

        # Make sure that if we have a deferred object it is completed
        task.id  # noqa

        try:
            WeightsFileHandler._model_store_lookup_lock.acquire()

            # # disable model reuse, let Model module try to find it for use
            trains_out_model, ref_model = None, None  # noqa: F841
            # check if object already has InputModel
            # trains_out_model, ref_model = WeightsFileHandler._model_out_store_lookup.get(
            #     id(model) if model is not None else None, (None, None))
            # # notice ref_model() is not an error/typo this is a weakref object call
            # # noinspection PyCallingNonCallable
            # if ref_model is not None and model != ref_model():
            #     # old id pop it - it was probably reused because the object is dead
            #     WeightsFileHandler._model_out_store_lookup.pop(id(model))
            #     trains_out_model, ref_model = None, None

            try:
                local_model_path = os.path.abspath(saved_path) if saved_path else saved_path
            except TypeError:
                # not a recognized type:
                return saved_path

            model_info = WeightsFileHandler.ModelInfo(
                model=trains_out_model,
                upload_filename=None,
                local_model_path=local_model_path,
                local_model_id=saved_path,
                framework=framework,
                task=task,
            )

            if not model_info.local_model_path:
                # get_logger(TrainsFrameworkAdapter).debug(
                #     "Could not retrieve model location, skipping auto model logging")
                return saved_path

            # check if we have output storage, and generate list of files to upload
            if Path(model_info.local_model_path).is_dir():
                files = [str(f) for f in Path(model_info.local_model_path).rglob("*")]
            elif singlefile:
                files = [str(Path(model_info.local_model_path).absolute())]
            else:
                files = [
                    str(f)
                    for f in Path(model_info.local_model_path).parent.glob(
                        str(Path(model_info.local_model_path).name) + ".*"
                    )
                ]

            target_filename = None
            if len(files) > 1:
                # noinspection PyBroadException
                try:
                    target_filename = Path(model_info.local_model_path).stem
                except Exception:
                    pass
            else:
                target_filename = Path(files[0]).name

            # pass model object to ModelInfo object, maybe someone can use it
            model_info.weights_object = model
            # call pre model callback functions
            model_info.upload_filename = target_filename
            for cb in list(WeightsFileHandler._model_pre_callbacks.values()):
                # noinspection PyBroadException
                try:
                    model_info = cb(WeightsFileHandler.CallbackType.save, model_info)
                except Exception:
                    pass
            # making sure we do not store an additional reference to the original model
            model_info.weights_object = None

            # if callbacks force us to leave they return None
            if model_info is None:
                # callback forced quit
                return saved_path

            # update the trains_out_model after the pre callbacks
            trains_out_model = model_info.model

            # check if object already has InputModel
            if trains_out_model is None:
                # noinspection PyProtectedMember
                in_model_id, model_uri = Model._local_model_to_id_uri.get(
                    model_info.local_model_id or model_info.local_model_path,
                    (None, None),
                )

                if not in_model_id:
                    # if we are overwriting a local file, try to load registered model
                    # if there is an output_uri, then by definition we will not overwrite previously stored models.
                    if not task.output_uri:
                        # noinspection PyBroadException
                        try:
                            in_model_id = InputModel.load_model(weights_url=model_info.local_model_path)
                            if in_model_id:
                                in_model_id = in_model_id.id

                                get_logger(TrainsFrameworkAdapter).info(
                                    "Found existing registered model id={} [{}] reusing it.".format(
                                        in_model_id, model_info.local_model_path
                                    )
                                )
                        except Exception:
                            in_model_id = None
                    else:
                        in_model_id = None

                trains_out_model = OutputModel(
                    task=task,
                    config_dict=config_obj if isinstance(config_obj, dict) else None,
                    config_text=config_obj if isinstance(config_obj, str) else None,
                    name=None
                    if in_model_id
                    else "{} - {}".format(task.name, model_name or Path(model_info.local_model_path).stem),
                    label_enumeration=task.get_labels_enumeration(),
                    framework=framework,
                    base_model_id=in_model_id,
                )
                # # disable model reuse, let Model module try to find it for use
                # if model is not None:
                #     # noinspection PyBroadException
                #     try:
                #         ref_model = weakref.ref(model)
                #     except Exception:
                #         ref_model = None
                #     WeightsFileHandler._model_out_store_lookup[id(model)] = (trains_out_model, ref_model)

            model_info.model = trains_out_model
            # pass model object to ModelInfo object, maybe someone can use it
            model_info.weights_object = model
            # call post model callback functions
            for cb in list(WeightsFileHandler._model_post_callbacks.values()):
                # noinspection PyBroadException
                try:
                    model_info = cb(WeightsFileHandler.CallbackType.save, model_info)
                except Exception:
                    pass
            # making sure we do not store an additional reference to the original model
            model_info.weights_object = None

            trains_out_model = model_info.model
            target_filename = model_info.upload_filename

            # upload files if we found them, or just register the original path
            if trains_out_model.upload_storage_uri:
                if len(files) > 1:
                    trains_out_model.update_weights_package(
                        weights_filenames=files,
                        auto_delete_file=False,
                        target_filename=target_filename,
                    )
                else:
                    # create a copy of the stored file,
                    # protect against someone deletes/renames the file before async upload finish is done

                    # HACK: if pytorch-lightning is used, remove the temp '.part' file extension
                    if sys.modules.get("pytorch_lightning") and target_filename.lower().endswith(".part"):
                        target_filename = target_filename[: -len(".part")]
                    fd, temp_file = mkstemp(prefix=".clearml.upload_model_", suffix=".tmp")
                    os.close(fd)
                    shutil.copy(files[0], temp_file)
                    trains_out_model.update_weights(
                        weights_filename=temp_file,
                        auto_delete_file=True,
                        target_filename=target_filename,
                        update_comment=False,
                    )
            else:
                trains_out_model.update_weights(
                    weights_filename=None,
                    register_uri=model_info.local_model_path,
                    is_package=bool(len(files) > 1),
                )

            # update back the internal Model lookup, and replace the local file with our file
            # noinspection PyProtectedMember
            Model._local_model_to_id_uri[model_info.local_model_id] = (
                trains_out_model.id,
                trains_out_model.url,
            )

        except Exception as ex:
            get_logger(TrainsFrameworkAdapter).debug(str(ex))
        finally:
            WeightsFileHandler._model_store_lookup_lock.release()

        return saved_path
