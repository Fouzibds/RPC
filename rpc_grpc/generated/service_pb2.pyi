from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FactorialRequest(_message.Message):
    __slots__ = ("n",)
    N_FIELD_NUMBER: _ClassVar[int]
    n: int
    def __init__(self, n: _Optional[int] = ...) -> None: ...

class FactorialReply(_message.Message):
    __slots__ = ("n", "result", "digits", "compute_us")
    N_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    DIGITS_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_US_FIELD_NUMBER: _ClassVar[int]
    n: int
    result: str
    digits: int
    compute_us: float
    def __init__(self, n: _Optional[int] = ..., result: _Optional[str] = ..., digits: _Optional[int] = ..., compute_us: _Optional[float] = ...) -> None: ...

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
    __slots__ = ("id", "name", "category", "price", "stock", "warehouse", "tags", "dimensions", "supplier", "rating", "active", "updated_at_ms", "version", "description")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    STOCK_FIELD_NUMBER: _ClassVar[int]
    WAREHOUSE_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    DIMENSIONS_FIELD_NUMBER: _ClassVar[int]
    SUPPLIER_FIELD_NUMBER: _ClassVar[int]
    RATING_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    category: str
    price: float
    stock: int
    warehouse: str
    tags: _containers.RepeatedScalarFieldContainer[str]
    dimensions: Dimensions
    supplier: Supplier
    rating: float
    active: bool
    updated_at_ms: int
    version: int
    description: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., category: _Optional[str] = ..., price: _Optional[float] = ..., stock: _Optional[int] = ..., warehouse: _Optional[str] = ..., tags: _Optional[_Iterable[str]] = ..., dimensions: _Optional[_Union[Dimensions, _Mapping]] = ..., supplier: _Optional[_Union[Supplier, _Mapping]] = ..., rating: _Optional[float] = ..., active: _Optional[bool] = ..., updated_at_ms: _Optional[int] = ..., version: _Optional[int] = ..., description: _Optional[str] = ...) -> None: ...

class UpdateStockRequest(_message.Message):
    __slots__ = ("product_id", "delta", "idempotency_key")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    product_id: str
    delta: int
    idempotency_key: str
    def __init__(self, product_id: _Optional[str] = ..., delta: _Optional[int] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

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

class AnalyticsRequest(_message.Message):
    __slots__ = ("samples", "interval_ms")
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    samples: int
    interval_ms: int
    def __init__(self, samples: _Optional[int] = ..., interval_ms: _Optional[int] = ...) -> None: ...

class AnalyticsSnapshot(_message.Message):
    __slots__ = ("seq", "timestamp_ms", "total_units", "inventory_value", "low_stock_count", "orders_per_min", "hot_product_id", "operations")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_UNITS_FIELD_NUMBER: _ClassVar[int]
    INVENTORY_VALUE_FIELD_NUMBER: _ClassVar[int]
    LOW_STOCK_COUNT_FIELD_NUMBER: _ClassVar[int]
    ORDERS_PER_MIN_FIELD_NUMBER: _ClassVar[int]
    HOT_PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    seq: int
    timestamp_ms: int
    total_units: int
    inventory_value: float
    low_stock_count: int
    orders_per_min: float
    hot_product_id: str
    operations: int
    def __init__(self, seq: _Optional[int] = ..., timestamp_ms: _Optional[int] = ..., total_units: _Optional[int] = ..., inventory_value: _Optional[float] = ..., low_stock_count: _Optional[int] = ..., orders_per_min: _Optional[float] = ..., hot_product_id: _Optional[str] = ..., operations: _Optional[int] = ...) -> None: ...

class BulkUpdateSummary(_message.Message):
    __slots__ = ("applied", "rejected", "total_delta", "errors")
    APPLIED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_DELTA_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    applied: int
    rejected: int
    total_delta: int
    errors: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, applied: _Optional[int] = ..., rejected: _Optional[int] = ..., total_delta: _Optional[int] = ..., errors: _Optional[_Iterable[str]] = ...) -> None: ...

class StockLevel(_message.Message):
    __slots__ = ("product_id", "stock", "available", "warehouse")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    STOCK_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    WAREHOUSE_FIELD_NUMBER: _ClassVar[int]
    product_id: str
    stock: int
    available: bool
    warehouse: str
    def __init__(self, product_id: _Optional[str] = ..., stock: _Optional[int] = ..., available: _Optional[bool] = ..., warehouse: _Optional[str] = ...) -> None: ...
