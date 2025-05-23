from __future__ import division

import json
from typing import Union, Any

import pyparsing
import six

from .dicts import hocon_quote_key, hocon_unquote_key
from .pyhocon import ConfigFactory, HOCONConverter
from ..storage.util import parse_size


def parse_human_size(value: Any) -> Any:
    if isinstance(value, six.string_types):
        return parse_size(value)
    return value


def get_percentage(config: dict, key: str, required: bool = True, default: float = None) -> float:
    if required:
        value = config.get(key)
    else:
        value = config.get(key, default)
        if value is None:
            return
    try:
        if isinstance(value, six.string_types):
            value = value.strip()
            if value.endswith("%"):
                # "50%" => 0.5
                return float(value.strip("%")) / 100.0
            # "50" => 50

        value = float(value)
        if value < 1:
            # 0.5 => 50% => 0.5
            return value

        # 50 => 0.5, 10.5 => 0.105
        return value / 100.0

    except ValueError as e:
        raise ValueError("Config: failed parsing %s: %s" % (key, e))


def get_human_size_default(config: dict, key: str, default: Any = None) -> Any:
    raw_value = config.get(key, default)

    if raw_value is None:
        return default

    return parse_human_size(raw_value)


def config_dict_to_text(config: Union[str, dict, list]) -> str:
    # if already string return as is
    if isinstance(config, six.string_types):
        return config
    if not isinstance(config, (dict, list)):
        raise ValueError("Configuration only supports dictionary/list objects")
    try:
        # noinspection PyBroadException
        try:

            def raise_on_special_key(config_: dict) -> None:
                if not isinstance(config_, dict):
                    return
                special_chars = "$}[]:=+#`^?!@*&."
                for key in config_.keys():
                    if not isinstance(key, str):
                        continue
                    if any(key_char in special_chars for key_char in key):
                        raise ValueError(
                            "Configuration dictionary keys cannot contain any of the following characters: {}".format(
                                special_chars
                            )
                        )
                for val in config_.values():
                    raise_on_special_key(val)

            # will fall back to json+pyhocon
            raise_on_special_key(config)
            text = HOCONConverter.to_hocon(ConfigFactory.from_dict(hocon_quote_key(config)))
        except Exception:
            # fallback json+pyhocon
            # hack, pyhocon is not very good with dict conversion so we pass through json
            import json

            text = json.dumps(config)
            text = HOCONConverter.to_hocon(ConfigFactory.parse_string(text))

    except Exception:
        raise ValueError("Could not serialize configuration dictionary:\n", config)
    return text


def text_to_config_dict(text: six.string_types) -> dict:
    if not isinstance(text, six.string_types):
        raise ValueError("Configuration parsing only supports string")
    # noinspection PyBroadException
    try:
        return hocon_unquote_key(ConfigFactory.parse_string(text))
    except pyparsing.ParseBaseException as ex:
        pos = "at char {}, line:{}, col:{}".format(ex.loc, ex.lineno, ex.column)
        six.raise_from(
            ValueError("Could not parse configuration text ({}):\n{}".format(pos, text)),
            None,
        )
    except Exception:
        six.raise_from(ValueError("Could not parse configuration text:\n{}".format(text)), None)


def verify_basic_value(value: Any) -> bool:
    # return True if value of of basic type (json serializable)
    if not isinstance(
        value,
        six.string_types + six.integer_types + (six.text_type, float, list, tuple, dict, type(None)),
    ):
        return False
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False
