################################################################################
#
# Copyright (c) 2019, the Perspective Authors.
#
# This file is part of the Perspective library, distributed under the terms of
# the Apache License 2.0.  The full license can be found in the LICENSE file.
#

import datetime
import logging
import json
from functools import partial
from ..core.exception import PerspectiveError
from ..table import Table, PerspectiveCppError
from ..table._date_validator import _PerspectiveDateValidator

_date_validator = _PerspectiveDateValidator()


class DateTimeEncoder(json.JSONEncoder):
    """Before sending datetimes over the wire, convert them to Unix timestamps
    in milliseconds since epoch, using Perspective's date validator to
    ensure consistency."""

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return _date_validator.to_timestamp(obj)
        else:
            return super(DateTimeEncoder, self).default(obj)


class _PerspectiveManagerInternal(object):

    # Commands that should be blocked from execution when the manager is in
    # `locked` mode, i.e. its tables and views made immutable from remote
    # modification.
    LOCKED_COMMANDS = ["table", "update", "remove", "replace", "clear"]

    def _process(self, msg, post_callback, client_id=None):
        '''Given a message from the client, process it through the Perspective
        engine.

        Args:
            msg (:obj`dict`): a message from the client with instructions
                that map to engine operations
            post_callback (:obj`callable`): a function that returns data to the
                client. `post_callback` should be a callable that takes two
                parameters: `data` (str), and `binary` (bool), a kwarg that
                specifies whether `data` is a binary string.
        '''
        if isinstance(msg, str):
            if msg == "heartbeat":   # TODO fix this
                return
            msg = json.loads(msg)

        if not isinstance(msg, dict):
            raise PerspectiveError(
                "Message passed into `_process` should either be a "
                "JSON-serialized string or a dict.")

        cmd = msg["cmd"]

        if self._is_locked_command(msg) is True:
            error_string = "`{0}` failed - access denied".format(
                msg["cmd"] + (("." + msg["method"]) if msg.get("method", None) is not None else ""))
            error_message = self._make_error_message(msg["id"], error_string)
            post_callback(self._message_to_json(msg["id"], error_message))
            return

        try:
            if cmd == "init":
                # return empty response
                message = self._make_message(msg["id"], None)
                post_callback(self._message_to_json(msg["id"], message))
            elif cmd == "table":
                try:
                    # create a new Table and track it
                    data_or_schema = msg["args"][0]
                    self._tables[msg["name"]] = Table(
                        data_or_schema, **msg.get("options", {}))
                except IndexError:
                    self._tables[msg["name"]] = []
            elif cmd == "view":
                # create a new view and track it with the assigned client_id.
                new_view = self._tables[msg["table_name"]].view(
                    **msg.get("config", {}))
                new_view._client_id = client_id
                self._views[msg["view_name"]] = new_view
            elif cmd == "table_method" or cmd == "view_method":
                self._process_method_call(msg, post_callback, client_id)
        except(PerspectiveError, PerspectiveCppError) as e:
            # Catch errors and return them to client
            error_message = self._make_error_message(msg["id"], str(e))
            post_callback(self._message_to_json(msg["id"], error_message))

    def _process_method_call(self, msg, post_callback, client_id):
        '''When the client calls a method, validate the instance it calls on
        and return the result.
        '''
        if msg["cmd"] == "table_method":
            table_or_view = self._tables.get(msg["name"], None)
        else:
            table_or_view = self._views.get(msg["name"], None)
            if table_or_view is None:
                error_message = self._make_error_message(
                    msg["id"], "View is not initialized")
                post_callback(self._message_to_json(msg["id"], error_message))
        try:
            if msg.get("subscribe", False) is True:
                self._process_subscribe(
                    msg, table_or_view, post_callback, client_id)
            else:
                args = {}
                if msg["method"] in ("schema", "computed_schema", "get_computation_input_types"):
                    # make sure schema returns string types
                    args["as_string"] = True
                elif msg["method"].startswith("to_"):
                    # parse options in `to_format` calls
                    for d in msg.get("args", []):
                        args.update(d)
                else:
                    args = msg.get("args", [])

                if msg["method"] == "delete" and msg["cmd"] == "view_method":
                    # views can be removed, but tables cannot
                    self._views[msg["name"]].delete()
                    self._views.pop(msg["name"], None)
                    return

                if msg["method"].startswith("to_"):
                    # to_format takes dictionary of options
                    result = getattr(table_or_view, msg["method"])(**args)
                elif msg["method"] in ("update", "remove"):
                    # Apply first arg as position, then options dict as kwargs
                    data = args[0]
                    options = {}
                    if (len(args) > 1 and isinstance(args[1], dict)):
                        options = args[1]
                    result = getattr(table_or_view, msg["method"])(data, **options)
                elif msg["method"] in ("computed_schema", "get_computation_input_types"):
                    # these methods take args and kwargs
                    result = getattr(table_or_view, msg["method"])(*msg.get("args", []), **args)
                elif msg["method"] != "delete":
                    # otherwise parse args as list
                    result = getattr(table_or_view, msg["method"])(*args)
                if isinstance(result, bytes) and msg["method"] != "to_csv":
                    # return a binary to the client without JSON serialization,
                    # i.e. when we return an Arrow. If a method is added that
                    # returns a string, this condition needs to be updated as
                    # an Arrow binary is both `str` and `bytes` in Python 2.
                    self._process_bytes(result, msg, post_callback)
                else:
                    # return the result to the client
                    message = self._make_message(msg["id"], result)
                    post_callback(self._message_to_json(msg["id"], message))
        except Exception as error:
            message = self._make_error_message(msg["id"], str(error))
            post_callback(self._message_to_json(msg["id"], message))

    def _process_subscribe(self, msg, table_or_view, post_callback, client_id):
        '''When the client attempts to add or remove a subscription callback,
        validate and perform the requested operation.

        Args:
            msg (dict): the message from the client
            table_or_view {Table|View} : the instance that the subscription
                will be called on.
            post_callback (callable): a method that notifies the client with
                new data.
            client_id (str) : a unique str id that identifies the
                `PerspectiveSession` object that is passing the message.
        '''
        try:
            callback = None
            callback_id = msg.get("callback_id", None)
            method = msg.get("method", None)
            args = msg.get("args", [])
            if method and method[:2] == "on":
                # wrap the callback
                callback = partial(
                    self.callback, msg=msg, post_callback=post_callback)
                if callback_id:
                    self._callback_cache.add_callback({
                        "client_id": client_id,
                        "callback_id": callback_id,
                        "callback": callback,
                        "name": msg.get("name", None)
                    })
            elif callback_id is not None:
                # remove the callback with `callback_id`
                self._callback_cache.remove_callbacks(
                    lambda cb: cb["callback_id"] != callback_id)
            if callback is not None:
                # call the underlying method on the Table or View, and apply
                # the dictionary of kwargs that is passed through.
                if (method == "on_update"):
                    # If there are no arguments, make sure we call `on_update`
                    # with mode set to "none".
                    mode = {"mode": "none"}
                    if len(args) > 0:
                        mode = args[0]
                    getattr(table_or_view, method)(callback, **mode)
                else:
                    getattr(table_or_view, method)(callback)
            else:
                logging.info("callback not found for remote call {}".format(msg))
        except Exception as error:
            message = self._make_error_message(msg["id"], str(error))
            post_callback(self._message_to_json(msg["id"], message))

    def _process_bytes(self, binary, msg, post_callback):
        """Send a bytestring message to the client without attempting to
        serialize as JSON.

        Perspective's client expects two messages to be sent when a binary
        payload is expected: the first message is a JSON-serialized string with
        the message's `id` and `msg`, and the second message is a bytestring
        without any additional metadata. Implementations of the `post_callback`
        should have an optional kwarg named `binary`, which specifies whether
        `data` is a bytestring or not.

        Args:
            binary (bytes, bytearray) : a byte message to be passed to the client.
            msg (dict) : the original message that generated the binary
                response from Perspective.
            post_callback (callable) : a function that passes data to the
                client, with a `binary` (bool) kwarg that allows it to pass
                byte messages without serializing to JSON.
        """
        msg["is_transferable"] = True
        post_callback(json.dumps(msg, cls=DateTimeEncoder))
        post_callback(binary, binary=True)

    def callback(self, *args, **kwargs):
        '''Return a message to the client using the `post_callback` method.'''
        id = kwargs.get("msg")["id"]
        post_callback = kwargs.get("post_callback")
        # Coerce the message to be an object so it can be handled in
        # Javascript, where promises cannot be resolved with multiple args.
        updated = {
            "port_id": args[0],
        }
        msg = self._make_message(id, updated)
        if len(args) > 1 and type(args[1]) == bytes:
            self._process_bytes(args[1], msg, post_callback)
        else:
            post_callback(self._message_to_json(msg["id"], msg))

    def clear_views(self, client_id):
        '''Garbage collect views that belong to closed connections.'''
        count = 0
        names = []

        if not client_id:
            raise PerspectiveError("Cannot garbage collect views that are not linked to a specific client ID!")

        for name, view in self._views.items():
            if view._client_id == client_id:
                view.delete()
                names.append(name)
                count += 1

        for name in names:
            self._views.pop(name)

        logging.warning("GC {} views in memory".format(count))

    def _make_message(self, id, result):
        '''Return a serializable message for a successful result.'''
        return {
            "id": id,
            "data": result
        }

    def _make_error_message(self, id, error):
        '''Return a serializable message for an error result.'''
        return {
            "id": id,
            "error": error
        }

    def _message_to_json(self, id, message):
        '''Given a message object to be passed to Perspective, serialize it
        into a string using `DateTimeEncoder` and `allow_nan=False`.

        If an Exception occurs in serialization, catch the Exception and
        return an error message using `self._make_error_message`.

        Args:
            message (:obj:`dict`) a serializable message to be passed to
                Perspective.
        '''
        try:
            return json.dumps(message, allow_nan=False, cls=DateTimeEncoder)
        except ValueError as error:
            error_string = str(error)

            # Augment the default error message when invalid values are
            # detected, i.e. `NaN`
            if error_string == "Out of range float values are not JSON compliant":
                error_string = "Cannot serialize `NaN`, `Infinity` or `-Infinity` to JSON."

            error_message = self._make_error_message(id, "JSON serialization error: {}".format(error_string))

            logging.warning(error_message["error"])
            return json.dumps(error_message)

    def _is_locked_command(self, msg):
        '''Returns `True` if the manager instance is locked and the command
        is in `_PerspectiveManagerInternal.LOCKED_COMMANDS`, and `False` otherwise.'''
        if not self._lock:
            return False

        cmd = msg["cmd"]
        method = msg.get("method", None)

        if cmd == "table_method" and method == "delete":
            # table.delete is blocked, but view.delete is not
            return True

        return cmd == "table" or method in _PerspectiveManagerInternal.LOCKED_COMMANDS
