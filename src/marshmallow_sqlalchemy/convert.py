"""Type conversion system for mapping SQLAlchemy types to marshmallow fields.

This module provides the core type conversion functionality that powers automatic
field generation in marshmallow-sqlalchemy. The :class:`ModelConverter` class is
responsible for converting SQLAlchemy models and tables into dictionaries of
marshmallow fields.

**Key Components:**

- :class:`ModelConverter` - Main converter class for type mapping and field generation
- :func:`field_for` - Convenience function to generate a field for a specific model property
- :func:`column2field` - Convert a SQLAlchemy Column to a marshmallow field
- :func:`property2field` - Convert a SQLAlchemy property to a marshmallow field

**Type Mapping:**

The module uses a two-tier type mapping system:

1. **SQLA_TYPE_MAPPING**: Database-specific types (PostgreSQL UUID, MySQL YEAR, etc.)
2. **TYPE_MAPPING**: Standard Python types inherited from marshmallow (int, str, datetime, etc.)

**Custom Converters:**

You can create custom converters by subclassing :class:`ModelConverter` and customizing
the type mapping or conversion logic.

Example::

    from marshmallow_sqlalchemy import ModelConverter, SQLAlchemyAutoSchema
    from marshmallow import fields
    import sqlalchemy as sa


    class CustomConverter(ModelConverter):
        SQLA_TYPE_MAPPING = {
            **ModelConverter.SQLA_TYPE_MAPPING,
            sa.LargeBinary: fields.String,  # Treat binary as base64
        }


    class MySchema(SQLAlchemyAutoSchema):
        class Meta:
            model = MyModel
            model_converter = CustomConverter

.. seealso::
    - :class:`~marshmallow_sqlalchemy.schema.SQLAlchemySchema`
    - :class:`~marshmallow_sqlalchemy.schema.SQLAlchemyAutoSchema`
"""

from __future__ import annotations

import functools
import inspect
import uuid
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Union,
    cast,
    overload,
)

# Remove when dropping Python 3.9
try:
    from typing import TypeAlias, TypeGuard
except ImportError:
    from typing_extensions import TypeAlias, TypeGuard

import marshmallow as ma
import sqlalchemy as sa
from marshmallow import fields, validate
from sqlalchemy.dialects import mssql, mysql, postgresql
from sqlalchemy.orm import SynonymProperty

from .exceptions import ModelConversionError
from .fields import Related, RelatedList

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.declarative import DeclarativeMeta
    from sqlalchemy.orm import MapperProperty
    from sqlalchemy.types import TypeEngine

    PropertyOrColumn: TypeAlias = MapperProperty | sa.Column

_FieldPartial: TypeAlias = Callable[[], fields.Field]
# TODO: Use more specific type for second argument
_FieldClassFactory: TypeAlias = Callable[
    ["ModelConverter", Any], Union[type[fields.Field], _FieldPartial]
]


def _is_field(value: Any) -> TypeGuard[type[fields.Field]]:
    """Check if a value is a marshmallow Field class (not an instance).

    This type guard function is used internally to distinguish between Field classes
    and factory functions in the SQLA_TYPE_MAPPING dictionary.

    :param value: The value to check.
    :return: True if value is a Field class, False otherwise.
    """
    return isinstance(value, type) and issubclass(value, fields.Field)


def _base_column(column):
    """Unwrap proxied SQLAlchemy columns to get the underlying base column.

    SQLAlchemy sometimes wraps columns in proxies (e.g., in hybrid properties or
    association proxies). This function extracts the actual base column when there
    is exactly one base column that differs from the proxied column.

    :param column: A SQLAlchemy Column or column-like object.
    :return: The unwrapped base column, or the original column if no unwrapping is needed.
    """
    if column not in column.base_columns and len(column.base_columns) == 1:
        [base] = column.base_columns
        return base
    return column


def _has_default(column) -> bool:
    """Check if a SQLAlchemy column has a default value.

    A column is considered to have a default if it has any of:
    - A client-side default value (column.default)
    - A server-side default value (column.server_default)
    - Auto-increment behavior (for primary keys)

    This is used to determine if a field should be marked as required during
    deserialization. Columns with defaults are typically not required.

    :param column: A SQLAlchemy Column object.
    :return: True if the column has any form of default value, False otherwise.
    """
    return (
        column.default is not None
        or column.server_default is not None
        or _is_auto_increment(column)
    )


def _is_auto_increment(column) -> bool:
    """Check if a SQLAlchemy column is the auto-increment column for its table.

    Auto-increment columns (typically integer primary keys) automatically generate
    values when new rows are inserted, so they shouldn't be required during
    deserialization.

    :param column: A SQLAlchemy Column object.
    :return: True if the column is the table's auto-increment column, False otherwise.
    """
    return column.table is not None and column is column.table._autoincrement_column


def _list_field_factory(
    converter: ModelConverter, data_type: postgresql.ARRAY
) -> Callable[[], fields.List]:
    """Create a marshmallow List field factory for PostgreSQL ARRAY columns.

    This factory function generates an appropriate List field for PostgreSQL arrays,
    handling both single-dimensional and multi-dimensional arrays. The inner field
    type is determined by the array's item type.

    For multi-dimensional arrays (e.g., INTEGER[][]), this creates nested List fields
    to match the array dimensions.

    :param converter: ModelConverter instance used to determine the field class for array items.
    :param data_type: PostgreSQL ARRAY type containing item type and dimensions information.
    :return: A partial function that creates a configured List field when called.

    Example::

        # For INTEGER[] (1D array)
        # Returns: partial(fields.List, fields.Integer())

        # For INTEGER[][] (2D array)
        # Returns: partial(fields.List, fields.List(fields.Integer()))
    """
    FieldClass = converter._get_field_class_for_data_type(data_type.item_type)
    inner = FieldClass()
    if not data_type.dimensions or data_type.dimensions == 1:
        return functools.partial(fields.List, inner)

    # For multi-dimensional arrays, nest the Lists
    dimensions = data_type.dimensions
    for _ in range(dimensions - 1):
        inner = fields.List(inner)

    return functools.partial(fields.List, inner)


def _enum_field_factory(
    converter: ModelConverter, data_type: sa.Enum
) -> Callable[[], fields.Field]:
    """Create an appropriate marshmallow field factory for SQLAlchemy Enum columns.

    This factory determines whether to use a marshmallow Enum field (when the SQLAlchemy
    Enum has an associated Python enum class) or fall back to a Raw field (for string-based
    enums without an enum class).

    :param converter: ModelConverter instance (unused but required for factory signature).
    :param data_type: SQLAlchemy Enum type that may contain an enum_class attribute.
    :return: A partial function for Enum field creation, or the Raw field class.

    Example::

        # For sa.Enum(MyEnum) where MyEnum is a Python enum
        # Returns: partial(fields.Enum, enum=MyEnum)

        # For sa.Enum('open', 'closed') without enum_class
        # Returns: fields.Raw
    """
    return (
        functools.partial(fields.Enum, enum=data_type.enum_class)
        if data_type.enum_class
        else fields.Raw
    )


class ModelConverter:
    """Converts SQLAlchemy models and tables into marshmallow field dictionaries.

    The ModelConverter is responsible for the core type conversion logic in
    marshmallow-sqlalchemy. It maps SQLAlchemy column types to appropriate marshmallow
    field types and handles field configuration based on column properties.

    **Key Responsibilities:**

    - Convert SQLAlchemy columns to marshmallow fields
    - Map SQLAlchemy types to marshmallow field classes via SQLA_TYPE_MAPPING
    - Generate field kwargs based on column properties (nullable, default, length, etc.)
    - Handle relationships and foreign keys
    - Support custom type converters through subclassing

    **Type Mapping:**

    The converter uses two mapping dictionaries:

    1. ``SQLA_TYPE_MAPPING``: Maps SQLAlchemy-specific types (e.g., postgresql.UUID,
       mysql.YEAR) to marshmallow fields or factory functions.
    2. ``type_mapping``: Falls back to marshmallow's base TYPE_MAPPING for standard
       Python types (int -> Integer, str -> String, etc.).

    **Customization:**

    You can create a custom converter by subclassing and modifying the SQLA_TYPE_MAPPING
    or overriding conversion methods:

    Example::

        from marshmallow_sqlalchemy import ModelConverter
        from marshmallow import fields
        import sqlalchemy as sa


        class CustomConverter(ModelConverter):
            # Add custom type mapping
            SQLA_TYPE_MAPPING = {
                **ModelConverter.SQLA_TYPE_MAPPING,
                sa.LargeBinary: fields.String,  # Convert binary to base64 string
            }


        class MySchema(SQLAlchemyAutoSchema):
            class Meta:
                model = MyModel
                model_converter = CustomConverter

    :param schema_cls: Optional schema class to use for accessing custom TYPE_MAPPING.

    .. seealso::
        - :class:`SQLAlchemySchema` - Uses ModelConverter for field generation
        - :class:`SQLAlchemyAutoSchema` - Automatically generates all fields via converter
        - :func:`field_for` - Convenience function using default converter
    """

    SQLA_TYPE_MAPPING: dict[
        type[TypeEngine], type[fields.Field] | _FieldClassFactory
    ] = {
        sa.Enum: _enum_field_factory,
        sa.JSON: fields.Raw,
        sa.ARRAY: _list_field_factory,
        sa.PickleType: fields.Raw,
        postgresql.BIT: fields.Integer,
        postgresql.OID: fields.Integer,
        postgresql.UUID: fields.UUID,
        postgresql.MACADDR: fields.String,
        postgresql.INET: fields.String,
        postgresql.CIDR: fields.String,
        postgresql.JSON: fields.Raw,
        postgresql.JSONB: fields.Raw,
        postgresql.HSTORE: fields.Raw,
        postgresql.ARRAY: _list_field_factory,
        postgresql.MONEY: fields.Decimal,
        postgresql.DATE: fields.Date,
        postgresql.TIME: fields.Time,
        mysql.BIT: fields.Integer,
        mysql.YEAR: fields.Integer,
        mysql.SET: fields.List,
        mysql.ENUM: fields.Field,
        mysql.INTEGER: fields.Integer,
        mysql.DATETIME: fields.DateTime,
        mssql.BIT: fields.Integer,
        mssql.UNIQUEIDENTIFIER: fields.UUID,
    }
    DIRECTION_MAPPING = {"MANYTOONE": False, "MANYTOMANY": True, "ONETOMANY": True}

    def __init__(self, schema_cls: type[ma.Schema] | None = None):
        self.schema_cls = schema_cls

    @property
    def type_mapping(self) -> dict[type, type[fields.Field]]:
        if self.schema_cls:
            return self.schema_cls.TYPE_MAPPING
        return ma.Schema.TYPE_MAPPING

    def fields_for_model(
        self,
        model: type[DeclarativeMeta],
        *,
        include_fk: bool = False,
        include_relationships: bool = False,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        base_fields: dict | None = None,
        dict_cls: type[dict] = dict,
    ) -> dict[str, fields.Field]:
        """Generate marshmallow fields for all properties of a SQLAlchemy model.

        This method iterates through all mapped properties of the model (columns and
        relationships) and generates appropriate marshmallow fields for each one,
        respecting the inclusion/exclusion parameters.

        .. note::
            SynonymProperties are always ignored. If you need a field for a synonym,
            declare it explicitly in your schema.

        :param model: SQLAlchemy model class to generate fields from.
        :param include_fk: Whether to include foreign key columns as fields.
            Defaults to False.
        :param include_relationships: Whether to include relationship properties as fields.
            Defaults to False.
        :param fields: Optional whitelist of field names to include. If provided, only
            these fields will be generated.
        :param exclude: Optional blacklist of field names to exclude from generation.
        :param base_fields: Dictionary of explicitly declared fields that should not be
            auto-generated. If a field name appears here, it will be used as-is.
        :param dict_cls: Dictionary class to use for the returned mapping. Defaults to dict.
        :return: Dictionary mapping field names to marshmallow Field instances.
        """
        result = dict_cls()
        base_fields = base_fields or {}

        for prop in sa.inspect(model).attrs:  # type: ignore[union-attr]
            key = self._get_field_name(prop)
            if self._should_exclude_field(prop, fields=fields, exclude=exclude):
                # Allow marshmallow to validate and exclude the field key.
                result[key] = None
                continue
            if isinstance(prop, SynonymProperty):
                continue
            if hasattr(prop, "columns"):
                if not include_fk:
                    # Only skip a column if there is no overriden column
                    # which does not have a Foreign Key.
                    for column in prop.columns:
                        if not column.foreign_keys:
                            break
                    else:
                        continue
            if not include_relationships and hasattr(prop, "direction"):
                continue
            field = base_fields.get(key) or self.property2field(prop)
            if field:
                result[key] = field
        return result

    def fields_for_table(
        self,
        table: sa.Table,
        *,
        include_fk: bool = False,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        base_fields: dict | None = None,
        dict_cls: type[dict] = dict,
    ) -> dict[str, fields.Field]:
        """Generate marshmallow fields for all columns of a SQLAlchemy Table.

        This method iterates through all columns of the table and generates appropriate
        marshmallow fields for each one, respecting the inclusion/exclusion parameters.

        .. note::
            Unlike :meth:`fields_for_model`, this method cannot include relationships
            since Table objects don't have relationship definitions (use models for that).

        :param table: SQLAlchemy Table object to generate fields from.
        :param include_fk: Whether to include foreign key columns as fields.
            Defaults to False.
        :param fields: Optional whitelist of field names to include. If provided, only
            these fields will be generated.
        :param exclude: Optional blacklist of field names to exclude from generation.
        :param base_fields: Dictionary of explicitly declared fields that should not be
            auto-generated. If a field name appears here, it will be used as-is.
        :param dict_cls: Dictionary class to use for the returned mapping. Defaults to dict.
        :return: Dictionary mapping field names to marshmallow Field instances.
        """
        result = dict_cls()
        base_fields = base_fields or {}
        for column in table.columns:
            key = self._get_field_name(column)
            if self._should_exclude_field(column, fields=fields, exclude=exclude):
                # Allow marshmallow to validate and exclude the field key.
                result[key] = None
                continue
            if not include_fk and column.foreign_keys:
                continue
            # Overridden fields are specified relative to key generated by
            # self._get_key_for_column(...), rather than keys in source model
            field = base_fields.get(key) or self.column2field(column)
            if field:
                result[key] = field
        return result

    @overload
    def property2field(
        self,
        prop: MapperProperty,
        *,
        instance: Literal[True] = ...,
        field_class: type[fields.Field] | None = ...,
        **kwargs,
    ) -> fields.Field: ...

    @overload
    def property2field(
        self,
        prop: MapperProperty,
        *,
        instance: Literal[False] = ...,
        field_class: type[fields.Field] | None = ...,
        **kwargs,
    ) -> type[fields.Field]: ...

    def property2field(
        self,
        prop: MapperProperty,
        *,
        instance: bool = True,
        field_class: type[fields.Field] | None = None,
        **kwargs,
    ) -> fields.Field | type[fields.Field]:
        """Convert a SQLAlchemy `Property` to a field instance or class.

        :param Property prop: SQLAlchemy Property.
        :param bool instance: If `True`, return  `Field` instance, computing relevant kwargs
            from the given property. If `False`, return the `Field` class.
        :param kwargs: Additional keyword arguments to pass to the field constructor.
        :return: A `marshmallow.fields.Field` class or instance.
        """
        # handle synonyms
        # Attribute renamed "_proxied_object" in 1.4
        for attr in ("_proxied_property", "_proxied_object"):
            proxied_obj = getattr(prop, attr, None)
            if proxied_obj is not None:
                prop = proxied_obj
        field_class = field_class or self._get_field_class_for_property(prop)
        if not instance:
            return field_class
        field_kwargs = self._get_field_kwargs_for_property(prop)
        field_kwargs.update(kwargs)
        ret = field_class(**field_kwargs)
        if (
            hasattr(prop, "direction")
            and self.DIRECTION_MAPPING[prop.direction.name]
            and prop.uselist is True
        ):
            ret = RelatedList(ret, **{**self.get_base_kwargs(), **kwargs})
        return ret

    @overload
    def column2field(
        self, column, *, instance: Literal[True] = ..., **kwargs
    ) -> fields.Field: ...

    @overload
    def column2field(
        self, column, *, instance: Literal[False] = ..., **kwargs
    ) -> type[fields.Field]: ...

    def column2field(
        self, column, *, instance: bool = True, **kwargs
    ) -> fields.Field | type[fields.Field]:
        """Convert a SQLAlchemy `Column <sqlalchemy.schema.Column>` to a field instance or class.

        :param sqlalchemy.schema.Column column: SQLAlchemy Column.
        :param bool instance: If `True`, return  `Field` instance, computing relevant kwargs
            from the given property. If `False`, return the `Field` class.
        :return: A `marshmallow.fields.Field` class or instance.
        """
        field_class = self._get_field_class_for_column(column)
        if not instance:
            return field_class
        field_kwargs = self.get_base_kwargs()
        self._add_column_kwargs(field_kwargs, column)
        return field_class(**{**field_kwargs, **kwargs})

    @overload
    def field_for(
        self,
        model: type[DeclarativeMeta],
        property_name: str,
        *,
        instance: Literal[True] = ...,
        field_class: type[fields.Field] | None = ...,
        **kwargs,
    ) -> fields.Field: ...

    @overload
    def field_for(
        self,
        model: type[DeclarativeMeta],
        property_name: str,
        *,
        instance: Literal[False] = ...,
        field_class: type[fields.Field] | None = None,
        **kwargs,
    ) -> type[fields.Field]: ...

    def field_for(
        self,
        model: type[DeclarativeMeta],
        property_name: str,
        *,
        instance: bool = True,
        field_class: type[fields.Field] | None = None,
        **kwargs,
    ) -> fields.Field | type[fields.Field]:
        """Convert a property for a mapped SQLAlchemy class to a marshmallow `Field`.
        Example: ::

            date_created = field_for(Author, "date_created", dump_only=True)
            author = field_for(Book, "author")

        :param type model: A SQLAlchemy mapped class.
        :param str property_name: The name of the property to convert.
        :param kwargs: Extra keyword arguments to pass to `property2field`
        :return: A `marshmallow.fields.Field` class or instance.
        """
        target_model = model
        prop_name = property_name
        attr = getattr(model, property_name)
        remote_with_local_multiplicity = False
        if hasattr(attr, "remote_attr"):
            target_model = attr.target_class
            prop_name = attr.value_attr
            remote_with_local_multiplicity = attr.local_attr.prop.uselist
        prop: MapperProperty = sa.inspect(target_model).attrs.get(prop_name)  # type: ignore[union-attr]
        converted_prop = self.property2field(
            prop,
            # To satisfy type checking, need to pass a literal bool
            instance=True if instance else False,  # noqa: SIM210
            field_class=field_class,
            **kwargs,
        )
        if remote_with_local_multiplicity:
            return RelatedList(converted_prop, **{**self.get_base_kwargs(), **kwargs})
        return converted_prop

    def _get_field_name(self, prop_or_column: PropertyOrColumn) -> str:
        return prop_or_column.key

    def _get_field_class_for_column(self, column: sa.Column) -> type[fields.Field]:
        return self._get_field_class_for_data_type(column.type)

    def _get_field_class_for_data_type(
        self, data_type: TypeEngine
    ) -> type[fields.Field]:
        """Determine the marshmallow field class for a SQLAlchemy data type.

        This method implements the core type conversion logic. It searches for an appropriate
        marshmallow field class in this order:

        1. Check SQLA_TYPE_MAPPING for SQLAlchemy-specific types (using MRO)
        2. Fall back to type_mapping using the column's python_type
        3. Recursively check data_type.impl for wrapped types
        4. Raise ModelConversionError if no mapping found

        :param data_type: SQLAlchemy TypeEngine instance (e.g., Integer, String, UUID).
        :return: Marshmallow Field class appropriate for the data type.
        :raises ModelConversionError: If no field mapping can be found for the type.
        """
        field_cls: type[fields.Field] | _FieldPartial | None = None
        types = inspect.getmro(type(data_type))
        # First search for a field class from self.SQLA_TYPE_MAPPING
        for col_type in types:
            if col_type in self.SQLA_TYPE_MAPPING:
                field_or_factory = self.SQLA_TYPE_MAPPING[col_type]
                if _is_field(field_or_factory):
                    field_cls = field_or_factory
                else:
                    field_cls = cast(_FieldClassFactory, field_or_factory)(
                        self, data_type
                    )
                break
        else:
            # Try to find a field class based on the column's python_type
            try:
                python_type = data_type.python_type
            except NotImplementedError:
                python_type = None

            if python_type in self.type_mapping:
                field_cls = self.type_mapping[python_type]
            else:
                if hasattr(data_type, "impl"):
                    return self._get_field_class_for_data_type(data_type.impl)
                raise ModelConversionError(
                    f"Could not find field column of type {types[0]}."
                )
        return cast(type[fields.Field], field_cls)

    def _get_field_class_for_property(self, prop) -> type[fields.Field]:
        """Determine the marshmallow field class for a SQLAlchemy property.

        Properties can be either column-based (regular columns) or relationship-based.
        This method distinguishes between them and returns the appropriate field class.

        :param prop: SQLAlchemy MapperProperty (ColumnProperty or RelationshipProperty).
        :return: Related field for relationships, or column-appropriate field for columns.
        """
        field_cls: type[fields.Field]
        if hasattr(prop, "direction"):
            field_cls = Related
        else:
            column = _base_column(prop.columns[0])
            field_cls = self._get_field_class_for_column(column)
        return field_cls

    def _get_field_kwargs_for_property(self, prop: PropertyOrColumn) -> dict[str, Any]:
        kwargs = self.get_base_kwargs()
        if hasattr(prop, "columns"):
            column = _base_column(prop.columns[0])
            self._add_column_kwargs(kwargs, column)
            prop = column
        if hasattr(prop, "direction"):  # Relationship property
            self._add_relationship_kwargs(kwargs, prop)
        if getattr(prop, "doc", None):  # Useful for documentation generation
            kwargs["metadata"]["description"] = prop.doc
        return kwargs

    def _add_column_kwargs(self, kwargs: dict[str, Any], column: sa.Column) -> None:
        """Add keyword arguments to kwargs (in-place) based on the passed in
        `Column <sqlalchemy.schema.Column>`.
        """
        if hasattr(column, "nullable"):
            if column.nullable:
                kwargs["allow_none"] = True
            kwargs["required"] = not column.nullable and not _has_default(column)
        # If there is no nullable attribute, we are dealing with a property
        # that does not derive from the Column class. Mark as dump_only.
        else:
            kwargs["dump_only"] = True

        if hasattr(column.type, "enum_class") and column.type.enum_class is not None:
            kwargs["enum"] = column.type.enum_class
        elif hasattr(column.type, "enums") and not kwargs.get("dump_only"):
            kwargs["validate"].append(validate.OneOf(choices=column.type.enums))

        # Add a length validator if a max length is set on the column
        # Skip UUID columns
        # (see https://github.com/marshmallow-code/marshmallow-sqlalchemy/issues/54)
        if hasattr(column.type, "length") and not kwargs.get("dump_only"):
            column_length = column.type.length
            if column_length is not None:
                try:
                    python_type = column.type.python_type
                except (AttributeError, NotImplementedError):
                    python_type = None
                if not python_type or not issubclass(python_type, uuid.UUID):
                    kwargs["validate"].append(validate.Length(max=column_length))

        if getattr(column.type, "asdecimal", False):
            kwargs["places"] = getattr(column.type, "scale", None)

    def _add_relationship_kwargs(
        self, kwargs: dict[str, Any], prop: PropertyOrColumn
    ) -> None:
        """Add keyword arguments to kwargs (in-place) based on the passed in
        relationship `Property`.
        """
        nullable = True
        for pair in prop.local_remote_pairs:
            if not pair[0].nullable:
                if (
                    prop.uselist is True
                    or self.DIRECTION_MAPPING[prop.direction.name] is False
                ):
                    nullable = False
                break
        kwargs.update({"allow_none": nullable, "required": not nullable})

    def _should_exclude_field(
        self,
        column: PropertyOrColumn,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> bool:
        """Determine if a field should be excluded from schema generation.

        A field is excluded if:
        - A whitelist (fields) is provided and the field is not in it, OR
        - A blacklist (exclude) is provided and the field is in it

        :param column: SQLAlchemy property or column to check.
        :param fields: Optional whitelist of field names to include. If provided,
            only these fields will be included.
        :param exclude: Optional blacklist of field names to exclude.
        :return: True if the field should be excluded, False otherwise.
        """
        key = self._get_field_name(column)
        if fields and key not in fields:
            return True
        return bool(exclude and key in exclude)

    def get_base_kwargs(self):
        return {"validate": [], "metadata": {}}


default_converter = ModelConverter()

fields_for_model = default_converter.fields_for_model
property2field = default_converter.property2field
column2field = default_converter.column2field
field_for = default_converter.field_for
