.. _tutorial:

********
Tutorial
********

Welcome to the marshmallow-sqlalchemy tutorial! This guide will walk you through
everything you need to know to start using marshmallow-sqlalchemy effectively.

By the end of this tutorial, you'll understand how to:

- Install and set up marshmallow-sqlalchemy
- Create schemas from SQLAlchemy models
- Serialize (dump) model instances to dictionaries
- Deserialize (load) data back to model instances
- Work with relationships
- Customize field generation

Installation and Setup
======================

Installing marshmallow-sqlalchemy
----------------------------------

marshmallow-sqlalchemy is available on PyPI and can be installed using pip:

.. code-block:: shell

    $ pip install marshmallow-sqlalchemy

This will install marshmallow-sqlalchemy along with its dependencies:

- marshmallow (>= 3.18.0)
- SQLAlchemy (>= 1.4.40)

.. note::
    marshmallow-sqlalchemy supports Python 3.9+ and works with both SQLAlchemy 1.4
    and 2.x.

Setting Up Your Environment
----------------------------

For this tutorial, we'll create a simple example using an in-memory SQLite database.
First, let's set up our SQLAlchemy models:

.. tab-set::

    .. tab-item:: SQLAlchemy 2.x
        :sync: sqla2

        .. code-block:: python

            import sqlalchemy as sa
            from sqlalchemy.orm import (
                DeclarativeBase,
                Mapped,
                mapped_column,
                relationship,
                Session,
            )

            # Create an in-memory database
            engine = sa.create_engine("sqlite:///:memory:", echo=True)


            class Base(DeclarativeBase):
                pass


            class Author(Base):
                __tablename__ = "authors"

                id: Mapped[int] = mapped_column(primary_key=True)
                name: Mapped[str] = mapped_column(sa.String(100))
                email: Mapped[str] = mapped_column(sa.String(100), unique=True)
                bio: Mapped[str | None] = mapped_column(sa.Text)

                books: Mapped[list["Book"]] = relationship(back_populates="author")

                def __repr__(self):
                    return f"<Author(name={self.name!r})>"


            class Book(Base):
                __tablename__ = "books"

                id: Mapped[int] = mapped_column(primary_key=True)
                title: Mapped[str] = mapped_column(sa.String(200))
                isbn: Mapped[str | None] = mapped_column(sa.String(13))
                published_date: Mapped[sa.Date | None] = mapped_column(sa.Date)
                author_id: Mapped[int] = mapped_column(sa.ForeignKey("authors.id"))

                author: Mapped["Author"] = relationship(back_populates="books")

                def __repr__(self):
                    return f"<Book(title={self.title!r})>"


            # Create all tables
            Base.metadata.create_all(engine)

    .. tab-item:: SQLAlchemy 1.4
        :sync: sqla1

        .. code-block:: python

            import sqlalchemy as sa
            from sqlalchemy.orm import (
                declarative_base,
                relationship,
                Session,
            )

            # Create an in-memory database
            engine = sa.create_engine("sqlite:///:memory:", echo=True)

            Base = declarative_base()


            class Author(Base):
                __tablename__ = "authors"

                id = sa.Column(sa.Integer, primary_key=True)
                name = sa.Column(sa.String(100), nullable=False)
                email = sa.Column(sa.String(100), unique=True, nullable=False)
                bio = sa.Column(sa.Text)

                books = relationship("Book", back_populates="author")

                def __repr__(self):
                    return f"<Author(name={self.name!r})>"


            class Book(Base):
                __tablename__ = "books"

                id = sa.Column(sa.Integer, primary_key=True)
                title = sa.Column(sa.String(200), nullable=False)
                isbn = sa.Column(sa.String(13))
                published_date = sa.Column(sa.Date)
                author_id = sa.Column(sa.Integer, sa.ForeignKey("authors.id"), nullable=False)

                author = relationship("Author", back_populates="books")

                def __repr__(self):
                    return f"<Book(title={self.title!r})>"


            # Create all tables
            Base.metadata.create_all(engine)

.. important::
    Always define your SQLAlchemy models **before** creating your marshmallow schemas.
    This ensures that the SQLAlchemy mapper is properly configured.

Basic Model-to-Schema Mapping
==============================

Now that we have our models set up, let's create marshmallow schemas to serialize
and deserialize them.

Using SQLAlchemySchema with auto_field
---------------------------------------

The :class:`~marshmallow_sqlalchemy.SQLAlchemySchema` class allows you to explicitly
declare which fields you want in your schema using :func:`~marshmallow_sqlalchemy.auto_field`:

.. code-block:: python

    from marshmallow_sqlalchemy import SQLAlchemySchema, auto_field


    class AuthorSchema(SQLAlchemySchema):
        class Meta:
            model = Author
            load_instance = True  # Deserialize to model instances

        id = auto_field()
        name = auto_field()
        email = auto_field()
        bio = auto_field()


    class BookSchema(SQLAlchemySchema):
        class Meta:
            model = Book
            load_instance = True

        id = auto_field()
        title = auto_field()
        isbn = auto_field()
        published_date = auto_field()
        author_id = auto_field()

Let's break down what's happening:

- ``class Meta`` defines configuration options for the schema
- ``model = Author`` tells the schema which SQLAlchemy model to use
- ``load_instance = True`` means deserialization will create model instances
- ``auto_field()`` generates the appropriate marshmallow field based on the column type

.. tip::
    Use :class:`~marshmallow_sqlalchemy.SQLAlchemySchema` when you want explicit
    control over which fields to include or when you need to customize specific fields.

Automatic Schema Generation
============================

For models with many columns, explicitly declaring every field can be tedious.
:class:`~marshmallow_sqlalchemy.SQLAlchemyAutoSchema` automatically generates
fields for all columns:

.. code-block:: python

    from marshmallow_sqlalchemy import SQLAlchemyAutoSchema


    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            load_instance = True
            include_relationships = True  # Include the 'books' relationship


    class BookSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Book
            include_fk = True  # Include foreign key fields
            load_instance = True

With :class:`~marshmallow_sqlalchemy.SQLAlchemyAutoSchema`:

- All model columns automatically become schema fields
- ``include_fk = True`` includes foreign key columns (default: False)
- ``include_relationships = True`` includes relationship properties (default: False)

.. note::
    You can still override individual fields in an auto-schema by declaring them explicitly.

Serialization (Dumping Data)
=============================

Serialization converts SQLAlchemy model instances into Python dictionaries (typically
for JSON APIs).

Basic Serialization
-------------------

.. code-block:: python

    from datetime import date

    # Create some data
    with Session(engine) as session:
        author = Author(
            name="J.K. Rowling",
            email="jk@example.com",
            bio="British author, best known for Harry Potter",
        )
        book = Book(
            title="Harry Potter and the Philosopher's Stone",
            isbn="9780747532699",
            published_date=date(1997, 6, 26),
            author=author,
        )
        session.add(author)
        session.add(book)
        session.commit()

        # Serialize a single object
        author_schema = AuthorSchema()
        result = author_schema.dump(author)
        print(result)
        # Output:
        # {
        #     'id': 1,
        #     'name': 'J.K. Rowling',
        #     'email': 'jk@example.com',
        #     'bio': 'British author, best known for Harry Potter',
        #     'books': [1]  # Relationship serialized as primary key(s)
        # }

        # Serialize multiple objects
        authors = session.query(Author).all()
        results = author_schema.dump(authors, many=True)
        print(results)
        # Output: List of author dictionaries

Serialization Notes
-------------------

- Use ``schema.dump(obj)`` for a single object
- Use ``schema.dump(objects, many=True)`` for a list of objects
- Relationships are serialized as primary key values by default
- No database session is required for serialization (dump)

Deserialization (Loading Data)
===============================

Deserialization converts dictionaries (from JSON, forms, etc.) into SQLAlchemy
model instances or validated dictionaries.

Basic Deserialization
---------------------

When ``load_instance = True`` in your schema's Meta, deserialization creates
model instances:

.. code-block:: python

    with Session(engine) as session:
        author_schema = AuthorSchema()

        # Data from a client (e.g., JSON API request)
        author_data = {
            "name": "George R.R. Martin",
            "email": "grrm@example.com",
            "bio": "American novelist and screenwriter",
        }

        # Deserialize to a model instance
        new_author = author_schema.load(author_data, session=session)
        print(new_author)
        # Output: <Author(name='George R.R. Martin')>

        # Add to session and commit
        session.add(new_author)
        session.commit()

        print(f"Created author with ID: {new_author.id}")

.. important::
    When ``load_instance = True``, you must provide a session:

    - Pass ``session=session`` to the ``load()`` method, OR
    - Set ``sqla_session`` in the schema's Meta class

Without load_instance
----------------------

If you don't set ``load_instance = True``, deserialization returns a dictionary:

.. code-block:: python

    class AuthorDataSchema(SQLAlchemySchema):
        class Meta:
            model = Author
            # load_instance = False (default)

        name = auto_field()
        email = auto_field()
        bio = auto_field()


    schema = AuthorDataSchema()
    data = {"name": "Isaac Asimov", "email": "isaac@example.com"}
    result = schema.load(data)
    print(result)
    # Output: {'name': 'Isaac Asimov', 'email': 'isaac@example.com'}
    # Returns a dict, not a model instance

Working with Relationships
===========================

marshmallow-sqlalchemy provides special handling for SQLAlchemy relationships.

Serializing Relationships
--------------------------

By default, relationships are serialized as primary key values:

.. code-block:: python

    class BookSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Book
            include_relationships = True
            load_instance = True


    with Session(engine) as session:
        book = session.query(Book).first()
        schema = BookSchema()
        result = schema.dump(book)
        print(result)
        # {
        #     'id': 1,
        #     'title': 'Harry Potter...',
        #     'author': 1,  # Just the primary key
        #     ...
        # }

Nested Relationships
--------------------

To serialize full nested objects, use marshmallow's ``Nested`` field:

.. code-block:: python

    from marshmallow import fields


    class BookWithAuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Book
            include_fk = True
            load_instance = True

        author = fields.Nested(AuthorSchema, exclude=("books",))


    with Session(engine) as session:
        book = session.query(Book).first()
        schema = BookWithAuthorSchema()
        result = schema.dump(book)
        print(result)
        # {
        #     'id': 1,
        #     'title': 'Harry Potter...',
        #     'author': {
        #         'id': 1,
        #         'name': 'J.K. Rowling',
        #         'email': 'jk@example.com',
        #         ...
        #     },
        #     ...
        # }

.. warning::
    Be careful with circular references! In the example above, we exclude
    ``'books'`` from the nested AuthorSchema to avoid infinite recursion.

Deserializing Relationships
----------------------------

When loading data with relationships, provide the related object's primary key:

.. code-block:: python

    with Session(engine) as session:
        # Assuming author with id=1 exists
        book_data = {
            "title": "A Game of Thrones",
            "isbn": "9780553103540",
            "author_id": 1,  # Reference existing author
        }

        book_schema = BookSchema()
        new_book = book_schema.load(book_data, session=session)
        session.add(new_book)
        session.commit()

        print(f"Created book: {new_book.title} by {new_book.author.name}")

Customizing Generated Fields
=============================

You can customize auto-generated fields by overriding them or passing arguments
to :func:`~marshmallow_sqlalchemy.auto_field`.

Overriding Field Configuration
-------------------------------

.. code-block:: python

    from marshmallow import validates, ValidationError
    from marshmallow_sqlalchemy import SQLAlchemyAutoSchema, auto_field


    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            load_instance = True

        # Make email required and add custom validation
        email = auto_field(required=True)

        # Make bio dump-only (won't accept it during load)
        bio = auto_field(dump_only=True)

        @validates("email")
        def validate_email(self, value):
            if "@" not in value:
                raise ValidationError("Invalid email address")


    # Usage
    schema = AuthorSchema()

    # This will fail validation
    try:
        schema.load({"name": "Test", "email": "invalid"})
    except ValidationError as err:
        print(err.messages)
        # {'email': ['Invalid email address']}

Common Field Customizations
----------------------------

.. code-block:: python

    class BookSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Book
            load_instance = True

        # Make field required
        title = auto_field(required=True)

        # Serialize only (don't accept during load)
        id = auto_field(dump_only=True)

        # Load only (don't include in serialization)
        # password = auto_field(load_only=True)

        # Add custom validation
        isbn = auto_field(validate=lambda x: len(x) == 13)

        # Rename field
        publication_date = auto_field(column_name="published_date")

Excluding Fields
----------------

You can exclude fields from auto-generation:

.. code-block:: python

    class AuthorPublicSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            exclude = ["email", "bio"]  # Don't include these fields


    # Or use 'fields' to create a whitelist
    class AuthorMinimalSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            fields = ["id", "name"]  # Only include these fields

Understanding load_instance
============================

The ``load_instance`` option controls whether deserialization creates model
instances or returns dictionaries.

With load_instance = True
--------------------------

.. code-block:: python

    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            load_instance = True  # Return model instances


    with Session(engine) as session:
        schema = AuthorSchema()

        data = {"name": "Neil Gaiman", "email": "neil@example.com"}
        author = schema.load(data, session=session)

        # author is an Author instance
        print(type(author))  # <class 'Author'>
        print(author.name)  # 'Neil Gaiman'

        # Can be added directly to session
        session.add(author)
        session.commit()

Without load_instance (Default)
--------------------------------

.. code-block:: python

    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            # load_instance = False (default)


    schema = AuthorSchema()
    data = {"name": "Neil Gaiman", "email": "neil@example.com"}
    result = schema.load(data)

    # result is a dictionary
    print(type(result))  # <class 'dict'>
    print(result["name"])  # 'Neil Gaiman'

    # To create a model, you'd do:
    # author = Author(**result)

Session Management
------------------

When ``load_instance = True``, you need a session. There are two ways to provide it:

**Option 1: Pass session to load()**

.. code-block:: python

    with Session(engine) as session:
        author = schema.load(data, session=session)

**Option 2: Set sqla_session in Meta**

.. code-block:: python

    from sqlalchemy.orm import scoped_session, sessionmaker

    SessionFactory = scoped_session(sessionmaker(bind=engine))


    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            load_instance = True
            sqla_session = SessionFactory


    # No need to pass session to load()
    schema = AuthorSchema()
    author = schema.load(data)

Transient Instances
-------------------

Set ``transient = True`` to create instances without attaching them to a session:

.. code-block:: python

    class AuthorSchema(SQLAlchemyAutoSchema):
        class Meta:
            model = Author
            load_instance = True
            transient = True  # Don't attach to session


    schema = AuthorSchema()
    author = schema.load(data)

    # author is not attached to any session
    # Useful for testing or when you want manual control

What's Next?
============

Congratulations! You now have a solid foundation in marshmallow-sqlalchemy.

Here are some next steps to deepen your knowledge:

**Advanced Topics**

- :ref:`recipes` - Common patterns and advanced use cases
- :ref:`API Reference <api>` - Complete API documentation
- Custom :class:`~marshmallow_sqlalchemy.ModelConverter` - Handle custom SQLAlchemy types

**Integration Examples**

- Using with Flask and Flask-SQLAlchemy
- Building REST APIs with marshmallow-sqlalchemy
- Testing schemas with pytest

**Best Practices**

- Schema organization in larger projects
- Handling circular schema references
- Performance optimization for large datasets
- Validation strategies

**Common Patterns**

- Base schema pattern for shared configuration
- Dynamic field exclusion based on user permissions
- Pagination with schemas
- Handling many-to-many relationships

.. seealso::
    - `marshmallow documentation <https://marshmallow.readthedocs.io/>`_
    - `SQLAlchemy documentation <https://docs.sqlalchemy.org/>`_
    - `marshmallow-sqlalchemy GitHub repository <https://github.com/marshmallow-code/marshmallow-sqlalchemy>`_

Happy coding! If you have questions, feel free to `open an issue
<https://github.com/marshmallow-code/marshmallow-sqlalchemy/issues>`_ on GitHub.
