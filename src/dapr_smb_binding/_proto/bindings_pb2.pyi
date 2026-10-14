from dapr_smb_binding._proto import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListOperationsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListOperationsResponse(_message.Message):
    __slots__ = ("operations",)
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    operations: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, operations: _Optional[_Iterable[str]] = ...) -> None: ...

class InputBindingInitRequest(_message.Message):
    __slots__ = ("metadata",)
    METADATA_FIELD_NUMBER: _ClassVar[int]
    metadata: _common_pb2.MetadataRequest
    def __init__(self, metadata: _Optional[_Union[_common_pb2.MetadataRequest, _Mapping]] = ...) -> None: ...

class InputBindingInitResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class OutputBindingInitRequest(_message.Message):
    __slots__ = ("metadata",)
    METADATA_FIELD_NUMBER: _ClassVar[int]
    metadata: _common_pb2.MetadataRequest
    def __init__(self, metadata: _Optional[_Union[_common_pb2.MetadataRequest, _Mapping]] = ...) -> None: ...

class OutputBindingInitResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AckResponseError(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...

class ReadRequest(_message.Message):
    __slots__ = ("response_data", "message_id", "response_error")
    RESPONSE_DATA_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ERROR_FIELD_NUMBER: _ClassVar[int]
    response_data: bytes
    message_id: str
    response_error: AckResponseError
    def __init__(self, response_data: _Optional[bytes] = ..., message_id: _Optional[str] = ..., response_error: _Optional[_Union[AckResponseError, _Mapping]] = ...) -> None: ...

class ReadResponse(_message.Message):
    __slots__ = ("data", "metadata", "content_type", "message_id")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    DATA_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    metadata: _containers.ScalarMap[str, str]
    content_type: str
    message_id: str
    def __init__(self, data: _Optional[bytes] = ..., metadata: _Optional[_Mapping[str, str]] = ..., content_type: _Optional[str] = ..., message_id: _Optional[str] = ...) -> None: ...

class InvokeRequest(_message.Message):
    __slots__ = ("data", "metadata", "operation")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    DATA_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    metadata: _containers.ScalarMap[str, str]
    operation: str
    def __init__(self, data: _Optional[bytes] = ..., metadata: _Optional[_Mapping[str, str]] = ..., operation: _Optional[str] = ...) -> None: ...

class InvokeResponse(_message.Message):
    __slots__ = ("data", "metadata", "content_type")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    DATA_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    metadata: _containers.ScalarMap[str, str]
    content_type: str
    def __init__(self, data: _Optional[bytes] = ..., metadata: _Optional[_Mapping[str, str]] = ..., content_type: _Optional[str] = ...) -> None: ...
