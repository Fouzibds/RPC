from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FactorialRequest(_message.Message):
    __slots__ = ("n", "use_cache")
    N_FIELD_NUMBER: _ClassVar[int]
    USE_CACHE_FIELD_NUMBER: _ClassVar[int]
    n: int
    use_cache: bool
    def __init__(self, n: _Optional[int] = ..., use_cache: _Optional[bool] = ...) -> None: ...

class FactorialReply(_message.Message):
    __slots__ = ("n", "result", "digits", "compute_us", "algorithm")
    N_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    DIGITS_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_US_FIELD_NUMBER: _ClassVar[int]
    ALGORITHM_FIELD_NUMBER: _ClassVar[int]
    n: int
    result: str
    digits: int
    compute_us: float
    algorithm: str
    def __init__(self, n: _Optional[int] = ..., result: _Optional[str] = ..., digits: _Optional[int] = ..., compute_us: _Optional[float] = ..., algorithm: _Optional[str] = ...) -> None: ...

class ProductRequest(_message.Message):
    __slots__ = ("product_id",)
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    product_id: str
    def __init__(self, product_id: _Optional[str] = ...) -> None: ...

class Dimensions(_message.Message):
    __slots__ = ("width_cm", "height_cm", "depth_cm", "weight_kg")
    WIDTH_CM_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_CM_FIELD_NUMBER: _ClassVar[int]
    DEPTH_CM_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_KG_FIELD_NUMBER: _ClassVar[int]
    width_cm: float
    height_cm: float
    depth_cm: float
    weight_kg: float
    def __init__(self, width_cm: _Optional[float] = ..., height_cm: _Optional[float] = ..., depth_cm: _Optional[float] = ..., weight_kg: _Optional[float] = ...) -> None: ...

class Supplier(_message.Message):
    __slots__ = ("name", "country", "lead_time_days")
    NAME_FIELD_NUMBER: _ClassVar[int]
    COUNTRY_FIELD_NUMBER: _ClassVar[int]
    LEAD_TIME_DAYS_FIELD_NUMBER: _ClassVar[int]
    name: str
    country: str
    lead_time_days: int
    def __init__(self, name: _Optional[str] = ..., country: _Optional[str] = ..., lead_time_days: _Optional[int] = ...) -> None: ...

class Product(_message.Message):
    __slots__ = ("id", "name", "category", "price_cents", "reserved", "warehouse", "tags", "dimensions", "supplier", "rating", "active", "updated_at_ms", "version", "description", "stock")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    PRICE_CENTS_FIELD_NUMBER: _ClassVar[int]
    RESERVED_FIELD_NUMBER: _ClassVar[int]
    WAREHOUSE_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    DIMENSIONS_FIELD_NUMBER: _ClassVar[int]
    SUPPLIER_FIELD_NUMBER: _ClassVar[int]
    RATING_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    STOCK_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    category: str
    price_cents: int
    reserved: int
    warehouse: str
    tags: _containers.RepeatedScalarFieldContainer[str]
    dimensions: Dimensions
    supplier: Supplier
    rating: float
    active: bool
    updated_at_ms: int
    version: int
    description: str
    stock: int
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., category: _Optional[str] = ..., price_cents: _Optional[int] = ..., reserved: _Optional[int] = ..., warehouse: _Optional[str] = ..., tags: _Optional[_Iterable[str]] = ..., dimensions: _Optional[_Union[Dimensions, _Mapping]] = ..., supplier: _Optional[_Union[Supplier, _Mapping]] = ..., rating: _Optional[float] = ..., active: _Optional[bool] = ..., updated_at_ms: _Optional[int] = ..., version: _Optional[int] = ..., description: _Optional[str] = ..., stock: _Optional[int] = ...) -> None: ...

class UpdateStockRequest(_message.Message):
    __slots__ = ("product_id", "warehouse", "idempotency_key", "delta")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    WAREHOUSE_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    product_id: str
    warehouse: str
    idempotency_key: str
    delta: int
    def __init__(self, product_id: _Optional[str] = ..., warehouse: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., delta: _Optional[int] = ...) -> None: ...

class UpdateStockReply(_message.Message):
    __slots__ = ("product_id", "previous_stock", "new_stock", "delta", "version", "applied", "updated_at_ms")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_STOCK_FIELD_NUMBER: _ClassVar[int]
    NEW_STOCK_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    APPLIED_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    product_id: str
    previous_stock: int
    new_stock: int
    delta: int
    version: int
    applied: bool
    updated_at_ms: int
    def __init__(self, product_id: _Optional[str] = ..., previous_stock: _Optional[int] = ..., new_stock: _Optional[int] = ..., delta: _Optional[int] = ..., version: _Optional[int] = ..., applied: _Optional[bool] = ..., updated_at_ms: _Optional[int] = ...) -> None: ...

class ListProductsRequest(_message.Message):
    __slots__ = ("limit", "category")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    limit: int
    category: str
    def __init__(self, limit: _Optional[int] = ..., category: _Optional[str] = ...) -> None: ...

class ProductList(_message.Message):
    __slots__ = ("products", "total")
    PRODUCTS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    products: _containers.RepeatedCompositeFieldContainer[Product]
    total: int
    def __init__(self, products: _Optional[_Iterable[_Union[Product, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...
