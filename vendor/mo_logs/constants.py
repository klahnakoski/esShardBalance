# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import, division, unicode_literals

import sys

from mo_dots import _set_attr as mo_dots_set_attr, set_attr as mo_dots_set_attr, split_field, wrap

DEBUG = True


def set(constants):
    """
    REACH INTO THE MODULES AND OBJECTS TO SET CONSTANTS.
    THINK OF THIS AS PRIMITIVE DEPENDENCY INJECTION FOR MODULES.
    USEFUL FOR SETTING DEBUG FLAGS.
    """
    if not constants:
        return
    constants = wrap(constants)

    for k, new_value in constants.leaves():
        errors = []
        try:
            old_value = mo_dots_set_attr(sys.modules, split_field(k), new_value)
            continue
        except Exception as e:
            errors.append(e)

        # ONE MODULE IS MISSING, THE CALLING MODULE
        try:
            caller_globals = sys._getframe(1).f_globals
            caller_file = caller_globals["__file__"]
            if not caller_file.endswith(".py"):
                raise Exception("do not know how to handle non-python caller")
            caller_module = caller_file[:-3].replace("\\", "/")
            path = caller_module.split("/")
            name = path[-1]

            old_value = mo_dots_set_attr(caller_globals, [name], new_value)
            if DEBUG:
                from mo_logs import Log

                Log.note(
                    "Changed {{module}}[{{attribute}}] from {{old_value}} to {{new_value}}",
                    module=caller_module,
                    attribute=name,
                    old_value=old_value,
                    new_value=new_value
                )
            break
        except Exception as e:
            errors.append(e)

        if errors:
            from mo_logs import Log

            Log.error("Can not set constant {{path}}", path=k, cause=errors)
