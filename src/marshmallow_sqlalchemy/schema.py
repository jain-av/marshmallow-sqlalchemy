from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from marshmallow.fields import Field
from marshmallow.schema import Schema, SchemaMeta, SchemaOpts, _get_fields

from .convert import ModelConverter
from .exceptions import IncorrectSchemaTypeError
from .load_instance_mixin import LoadInstanceMixin, _ModelType

if TYPE_CHECKING:
    from sqlalchemy.ext.declarative import DeclarativeMeta


# This isn't really a field; it's a placeholder for the metaclass.
# This should be considered private API.
class SQLAlchemyAutoField(Field):
    """Placeholder field that is replaced by the schema metaclass during schema creation.

    This is not a real marshmallow field. Instead, it acts as a marker that tells the
    schema metaclass to automatically generate the appropriate field based on the
    SQLAlchemy model's column or table definition.

    When you use :func:`auto_field` in a schema definition, it creates an instance of
    this class. During schema class creation, the metaclass processes these placeholder
    fields and replaces them with actual marshmallow field instances (String, Integer,
    Related, etc.) based on the SQLAlchemy column type.

    .. note::
        This class is part of the internal API and should not be instantiated directly.
        Use the :func:`auto_field` function instead.

    :param column_name: Name of the SQLAlchemy column to generate the field from.
        If None, uses the field's attribute name on the schema.
    :param model: SQLAlchemy model to use for field generation. If None, uses the
        model specified in the schema's Meta class.
    :param table: SQLAlchemy Table to use for field generation. If None, uses the
        table specified in the schema's Meta class.
    :param field_kwargs: Additional keyword arguments to pass to the generated field.

    .. seealso::
        - :func:`auto_field` - Public API for creating auto-generated fields
        - :class:`SQLAlchemySchema` - Schema class that uses auto_field
        - :class:`ModelConverter` - Handles the actual field generation
    """

    def __init__(
        self,
        *,
        column_name: str | None = None,
        model: type[DeclarativeMeta] | None = None,
        table: sa.Table | None = None,
        field_kwargs: dict[str, Any],
    ):
        super().__init__()

        if model and table:
            raise ValueError("Cannot pass both `model` and `table` options.")

        self.column_name = column_name
        self.model = model
        self.table = table
        self.field_kwargs = field_kwargs

    def create_field(
        self,
        schema_opts: SQLAlchemySchemaOpts,
        column_name: str,
        converter: ModelConverter,
    ):
        """Generate the actual marshmallow field from the SQLAlchemy model or table.

        This method is called by the schema metaclass during schema creation to replace
        the placeholder SQLAlchemyAutoField with a real marshmallow field instance.

        :param schema_opts: Schema options containing the model or table to use.
        :param column_name: Name of the column to generate the field from.
        :param converter: ModelConverter instance to use for field generation.
        :return: A marshmallow Field instance appropriate for the column type.
        """
        model = self.model or schema_opts.model
        if model:
            return converter.field_for(model, column_name, **self.field_kwargs)
        table = self.table if self.table is not None else schema_opts.table
        column = getattr(cast(sa.Table, table).columns, column_name)
        return converter.column2field(column, **self.field_kwargs)

    # This field should never be bound to a schema.
    # If this method is called, it's probably because the schema is not a SQLAlchemySchema.
    def _bind_to_schema(self, field_name: str, parent: Schema | Field) -> None:
        raise IncorrectSchemaTypeError(
            f"Cannot bind SQLAlchemyAutoField. Make sure that {parent} is a SQLAlchemySchema or SQLAlchemyAutoSchema."
        )


class SQLAlchemySchemaOpts(LoadInstanceMixin.Opts, SchemaOpts):
    """Options class for `SQLAlchemySchema`.
    Adds the following options:

    - ``model``: The SQLAlchemy model to generate the `Schema` from (mutually exclusive with ``table``).
    - ``table``: The SQLAlchemy table to generate the `Schema` from (mutually exclusive with ``model``).
    - ``load_instance``: Whether to load model instances.
    - ``sqla_session``: SQLAlchemy session to be used for deserialization.
        This is only needed when ``load_instance`` is `True`. You can also pass a session to the Schema's `load` method.
    - ``transient``: Whether to load model instances in a transient state (effectively ignoring the session).
        Only relevant when ``load_instance`` is `True`.
    - ``model_converter``: `ModelConverter` class to use for converting the SQLAlchemy model to marshmallow fields.
    """

    table: sa.Table | None
    model_converter: type[ModelConverter]

    def __init__(self, meta, *args, **kwargs):
        super().__init__(meta, *args, **kwargs)

        self.table = getattr(meta, "table", None)
        if self.model is not None and self.table is not None:
            raise ValueError("Cannot set both `model` and `table` options.")
        self.model_converter = getattr(meta, "model_converter", ModelConverter)


class SQLAlchemyAutoSchemaOpts(SQLAlchemySchemaOpts):
    """Options class for `SQLAlchemyAutoSchema`.
    Has the same options as `SQLAlchemySchemaOpts`, with the addition of:

    - ``include_fk``: Whether to include foreign fields; defaults to `False`.
    - ``include_relationships``: Whether to include relationships; defaults to `False`.
    """

    include_fk: bool
    include_relationships: bool

    def __init__(self, meta, *args, **kwargs):
        super().__init__(meta, *args, **kwargs)
        self.include_fk = getattr(meta, "include_fk", False)
        self.include_relationships = getattr(meta, "include_relationships", False)
        if self.table is not None and self.include_relationships:
            raise ValueError("Cannot set `table` and `include_relationships = True`.")


class SQLAlchemySchemaMeta(SchemaMeta):
    """Metaclass for SQLAlchemySchema that handles auto-field generation.

    This metaclass extends marshmallow's SchemaMeta to add support for SQLAlchemy-specific
    field generation. It processes :class:`SQLAlchemyAutoField` placeholders created by
    :func:`auto_field` and replaces them with appropriate marshmallow field instances
    based on the SQLAlchemy model or table definition.

    The metaclass also handles filtering of foreign key fields when ``include_fk=False``
    is set in the schema options.

    .. note::
        This is an internal metaclass. Users should work with :class:`SQLAlchemySchema`
        or :class:`SQLAlchemyAutoSchema` instead of using this metaclass directly.
    """

    @classmethod
    def get_declared_fields(
        mcs,
        klass,
        cls_fields: list[tuple[str, Field]],
        inherited_fields: list[tuple[str, Field]],
        dict_cls: type[dict] = dict,
    ) -> dict[str, Field]:
        """Collect and process all fields for the schema, including auto-generated ones.

        This method is called during schema class creation. It combines declared fields,
        inherited fields, SQLAlchemy-specific fields, and auto-generated fields into a
        single dictionary of field instances.

        :param klass: The schema class being created.
        :param cls_fields: Fields explicitly declared on the schema class.
        :param inherited_fields: Fields inherited from parent schema classes.
        :param dict_cls: Dictionary class to use for the returned field mapping.
        :return: Dictionary mapping field names to Field instances.
        """
        opts = klass.opts
        Converter: type[ModelConverter] = opts.model_converter
        converter = Converter(schema_cls=klass)
        fields = super().get_declared_fields(
            klass,
            cls_fields,
            # Filter out fields generated from foreign key columns
            # if include_fk is set to False in the options
            mcs._maybe_filter_foreign_keys(inherited_fields, opts=opts, klass=klass),
            dict_cls,
        )
        fields.update(mcs.get_declared_sqla_fields(fields, converter, opts, dict_cls))
        fields.update(mcs.get_auto_fields(fields, converter, opts, dict_cls))
        return fields

    @classmethod
    def get_declared_sqla_fields(
        mcs,
        base_fields: dict[str, Field],
        converter: ModelConverter,
        opts: Any,
        dict_cls: type[dict],
    ) -> dict[str, Field]:
        """Generate fields from SQLAlchemy model or table definition.

        For :class:`SQLAlchemySchema`, this returns an empty dictionary since fields
        must be explicitly declared with :func:`auto_field`. Subclasses like
        :class:`SQLAlchemyAutoSchemaMeta` override this to auto-generate all fields.

        :param base_fields: Already declared fields on the schema.
        :param converter: ModelConverter instance for field generation.
        :param opts: Schema options containing model/table and other settings.
        :param dict_cls: Dictionary class to use for the returned field mapping.
        :return: Dictionary of auto-generated fields (empty for base SQLAlchemySchema).
        """
        return {}

    @classmethod
    def get_auto_fields(
        mcs,
        fields: dict[str, Field],
        converter: ModelConverter,
        opts: Any,
        dict_cls: type[dict],
    ) -> dict[str, Field]:
        """Process and replace SQLAlchemyAutoField placeholders with real field instances.

        Iterates through all fields, finds instances of :class:`SQLAlchemyAutoField`,
        and calls their :meth:`~SQLAlchemyAutoField.create_field` method to generate
        the appropriate marshmallow field based on the SQLAlchemy column type.

        :param fields: Dictionary of all fields declared on the schema, including
            SQLAlchemyAutoField placeholders.
        :param converter: ModelConverter instance for field generation.
        :param opts: Schema options containing model/table and other settings.
        :param dict_cls: Dictionary class to use for the returned field mapping.
        :return: Dictionary mapping field names to generated Field instances.
        """
        return dict_cls(
            {
                field_name: field.create_field(
                    opts, field.column_name or field_name, converter
                )
                for field_name, field in fields.items()
                if isinstance(field, SQLAlchemyAutoField)
                and field_name not in opts.exclude
            }
        )

    @staticmethod
    def _maybe_filter_foreign_keys(
        fields: list[tuple[str, Field]],
        *,
        opts: SQLAlchemySchemaOpts,
        klass: SchemaMeta,
    ) -> list[tuple[str, Field]]:
        """Filter out foreign key fields from inherited fields if include_fk is False.

        When ``include_fk=False`` in schema options, this method removes fields that
        correspond to foreign key columns, unless those fields were explicitly declared
        (not auto-generated) in a parent schema class.

        :param fields: List of (name, field) tuples from parent schemas.
        :param opts: Schema options, containing include_fk setting and model/table.
        :param klass: The schema class being created.
        :return: Filtered list of fields with foreign key fields removed if appropriate.
        """
        if opts.model is not None or opts.table is not None:
            if not hasattr(opts, "include_fk") or opts.include_fk is True:
                return fields
            foreign_keys = {
                column.key
                for column in sa.inspect(opts.model or opts.table).columns  # type: ignore[union-attr]
                if column.foreign_keys
            }

            non_auto_schema_bases = [
                base
                for base in inspect.getmro(klass)
                if issubclass(base, Schema)
                and not issubclass(base, SQLAlchemyAutoSchema)
            ]

            def is_declared_field(field: str) -> bool:
                return any(
                    field
                    in [
                        name
                        for name, _ in _get_fields(
                            getattr(base, "_declared_fields", base.__dict__)
                        )
                    ]
                    for base in non_auto_schema_bases
                )

            return [
                (name, field)
                for name, field in fields
                if name not in foreign_keys or is_declared_field(name)
            ]
        return fields


class SQLAlchemyAutoSchemaMeta(SQLAlchemySchemaMeta):
    """Metaclass for SQLAlchemyAutoSchema that automatically generates all fields.

    This metaclass extends :class:`SQLAlchemySchemaMeta` to automatically generate
    marshmallow fields for all columns in a SQLAlchemy model or table, without requiring
    explicit :func:`auto_field` declarations.

    The behavior is controlled by schema Meta options:
    - ``include_fk``: Whether to include foreign key columns as fields
    - ``include_relationships``: Whether to include relationship properties as fields

    .. note::
        This is an internal metaclass. Users should work with :class:`SQLAlchemyAutoSchema`
        instead of using this metaclass directly.
    """

    @classmethod
    def get_declared_sqla_fields(
        cls, base_fields, converter: ModelConverter, opts, dict_cls
    ):
        """Auto-generate fields for all columns in the model or table.

        Overrides the parent method to automatically create fields for every column
        in the SQLAlchemy model or table, respecting the ``include_fk`` and
        ``include_relationships`` options.

        :param base_fields: Already declared fields on the schema.
        :param converter: ModelConverter instance for field generation.
        :param opts: Schema options containing model/table and generation settings.
        :param dict_cls: Dictionary class to use for the returned field mapping.
        :return: Dictionary of auto-generated fields for all model/table columns.
        """
        fields = dict_cls()
        if opts.table is not None:
            fields.update(
                converter.fields_for_table(
                    opts.table,
                    fields=opts.fields,
                    exclude=opts.exclude,
                    include_fk=opts.include_fk,
                    base_fields=base_fields,
                    dict_cls=dict_cls,
                )
            )
        elif opts.model is not None:
            fields.update(
                converter.fields_for_model(
                    opts.model,
                    fields=opts.fields,
                    exclude=opts.exclude,
                    include_fk=opts.include_fk,
                    include_relationships=opts.include_relationships,
                    base_fields=base_fields,
                    dict_cls=dict_cls,
                )
            )
        return fields


class SQLAlchemySchema(
    LoadInstanceMixin.Schema[_ModelType], Schema, metaclass=SQLAlchemySchemaMeta
):
    """Schema for a SQLAlchemy model or table with explicit field declarations.

    Use this schema class when you want fine-grained control over which fields to include
    and their configuration. Fields must be explicitly declared using :func:`auto_field`,
    which generates the appropriate marshmallow field based on the SQLAlchemy column type.

    This approach is useful when you only need a subset of model columns, want to customize
    field behavior, or need to maintain compatibility with specific marshmallow patterns.

    **Key Features:**

    - Explicit field control with :func:`auto_field`
    - Support for both SQLAlchemy models and tables
    - Optional instance deserialization with ``load_instance=True``
    - Session-aware loading and transient mode support
    - Customizable field generation via ``model_converter``

    **Meta Options:**

    - ``model``: SQLAlchemy model class (mutually exclusive with ``table``)
    - ``table``: SQLAlchemy Table object (mutually exclusive with ``model``)
    - ``load_instance``: If True, deserialize to model instances (default: False)
    - ``sqla_session``: SQLAlchemy session for loading instances
    - ``transient``: If True, create transient instances without session binding
    - ``model_converter``: Custom ModelConverter class for field generation

    Example: ::

        from marshmallow_sqlalchemy import SQLAlchemySchema, auto_field
        from mymodels import User


        class UserSchema(SQLAlchemySchema):
            class Meta:
                model = User
                load_instance = True

            id = auto_field()
            created_at = auto_field(dump_only=True)
            name = auto_field()
            email = auto_field()


        # Serialization
        schema = UserSchema()
        user = User(id=1, name="John", email="john@example.com")
        result = schema.dump(user)
        # {'id': 1, 'name': 'John', 'email': 'john@example.com', 'created_at': '2024-01-01T00:00:00'}

        # Deserialization to model instance
        data = {"name": "Jane", "email": "jane@example.com"}
        user_instance = schema.load(data, session=session)
        # <User(name='Jane')>

    .. seealso::
        - :class:`SQLAlchemyAutoSchema` - Automatically generates fields for all columns
        - :func:`auto_field` - Declares a field to be auto-generated
        - :class:`ModelConverter` - Handles SQLAlchemy type to marshmallow field conversion
    """

    OPTIONS_CLASS = SQLAlchemySchemaOpts


class SQLAlchemyAutoSchema(
    SQLAlchemySchema[_ModelType], metaclass=SQLAlchemyAutoSchemaMeta
):
    """Schema that automatically generates fields for all columns in a SQLAlchemy model or table.

    Use this schema class when you want to serialize/deserialize all or most columns from
    a SQLAlchemy model without explicitly declaring each field. Fields are automatically
    generated based on column types, with options to control foreign keys and relationships.

    This approach is convenient for CRUD APIs and when your schema closely mirrors your
    database model. You can still override or customize individual fields using
    :func:`auto_field` or by declaring regular marshmallow fields.

    **Key Features:**

    - Automatic field generation for all columns
    - Optional foreign key inclusion via ``include_fk``
    - Optional relationship serialization via ``include_relationships``
    - Selective field control with ``fields`` and ``exclude`` Meta options
    - Field customization by overriding specific fields in schema definition

    **Meta Options:**

    All options from :class:`SQLAlchemySchema`, plus:

    - ``include_fk``: Include foreign key columns as fields (default: False)
    - ``include_relationships``: Include relationship properties as fields (default: False)
    - ``fields``: Whitelist of field names to include (all others excluded)
    - ``exclude``: Blacklist of field names to exclude

    **When to use SQLAlchemyAutoSchema vs SQLAlchemySchema:**

    - Use ``SQLAlchemyAutoSchema`` when you want most/all model fields in your schema
    - Use ``SQLAlchemySchema`` when you only need a small subset of fields or want
      maximum control over field generation

    Example: ::

        from marshmallow_sqlalchemy import SQLAlchemyAutoSchema, auto_field
        from mymodels import User, Article


        # Auto-generate all fields
        class UserSchema(SQLAlchemyAutoSchema):
            class Meta:
                model = User
                load_instance = True
                include_relationships = True


        # Auto-generate with customization
        class ArticleSchema(SQLAlchemyAutoSchema):
            class Meta:
                model = Article
                include_fk = True
                exclude = ["internal_notes"]  # Exclude sensitive fields

            # Override auto-generated field
            created_at = auto_field(dump_only=True)


        # Using with tables instead of models
        class UserTableSchema(SQLAlchemyAutoSchema):
            class Meta:
                table = User.__table__
                include_fk = True

    .. note::
        When using ``table`` instead of ``model``, ``include_relationships`` must be
        False (the default) since tables don't have relationship properties.

    .. seealso::
        - :class:`SQLAlchemySchema` - For explicit field control
        - :func:`auto_field` - For customizing individual auto-generated fields
        - :class:`ModelConverter` - Handles type conversion logic
    """

    OPTIONS_CLASS = SQLAlchemyAutoSchemaOpts


def auto_field(
    column_name: str | None = None,
    *,
    model: type[DeclarativeMeta] | None = None,
    table: sa.Table | None = None,
    **kwargs: Any,
) -> SQLAlchemyAutoField:
    """Mark a field to be auto-generated from a SQLAlchemy model or table column.

    This function creates a placeholder that the schema metaclass will replace with an
    appropriate marshmallow field (String, Integer, DateTime, Related, etc.) based on
    the SQLAlchemy column type.

    Use this in :class:`SQLAlchemySchema` to explicitly declare which fields to include,
    or in :class:`SQLAlchemyAutoSchema` to customize auto-generated fields.

    :param column_name: Name of the SQLAlchemy column to generate the field from.
        If ``None``, uses the field's attribute name on the schema class.
        If ``attribute`` is not provided in kwargs, it will automatically be set
        to match ``column_name``.
    :param model: SQLAlchemy model class to use for field generation.
        If ``None``, uses the ``model`` specified in the schema's Meta class.
        Mutually exclusive with ``table``.
    :param table: SQLAlchemy Table object to use for field generation.
        If ``None``, uses the ``table`` specified in the schema's Meta class.
        Mutually exclusive with ``model``.
    :param kwargs: Additional keyword arguments passed to the generated marshmallow field.
        Common options include:

        - ``dump_only``: Field is only used for serialization
        - ``load_only``: Field is only used for deserialization
        - ``required``: Override auto-detected required status
        - ``allow_none``: Override auto-detected nullable status
        - ``validate``: Add custom validators
        - ``dump_default``: Default value when serializing
        - ``load_default``: Default value when deserializing

    :return: A SQLAlchemyAutoField placeholder that will be replaced during schema creation.

    Example: ::

        from marshmallow_sqlalchemy import SQLAlchemySchema, auto_field
        from mymodels import User


        class UserSchema(SQLAlchemySchema):
            class Meta:
                model = User

            # Basic auto-field
            id = auto_field()

            # With field customization
            email = auto_field(required=True, validate=validate.Email())

            # With dump_only (won't accept this field during load)
            created_at = auto_field(dump_only=True)

            # From a different column name
            user_name = auto_field(column_name="username")

            # From a specific model (useful in inheritance scenarios)
            admin_flag = auto_field(model=AdminUser)

    .. seealso::
        - :class:`SQLAlchemySchema` - Schema requiring explicit auto_field declarations
        - :class:`SQLAlchemyAutoSchema` - Auto-generates fields without explicit declarations
        - :class:`ModelConverter` - Performs the actual field generation
    """
    if column_name is not None:
        kwargs.setdefault("attribute", column_name)
    return SQLAlchemyAutoField(
        column_name=column_name, model=model, table=table, field_kwargs=kwargs
    )
