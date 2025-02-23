from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    backref,
    column_property,
    relationship,
    sessionmaker,
    synonym,
)
from sqlalchemy.orm import DeclarativeMeta

mapped_column: Any
try:
    from sqlalchemy.orm import mapped_column
except ImportError:  # compat with sqlalchemy<2
    mapped_column = sa.Column


class AnotherInteger(sa.Integer):
    """Use me to test if MRO works like we want"""


class AnotherText(sa.types.TypeDecorator):
    """Use me to test if MRO and `impl` virtual type works like we want"""

    impl = sa.UnicodeText


@pytest.fixture
def Base() -> type[DeclarativeBase]:
    class Base(DeclarativeBase):
        pass

    return Base


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite:///:memory:", echo=False, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def session(Base, models, engine):
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    with Session() as session:
        yield session


CourseLevel = Enum("CourseLevel", "PRIMARY SECONDARY")


@dataclass
class Models:
    Course: type[DeclarativeMeta]
    School: type[DeclarativeMeta]
    Student: type[DeclarativeMeta]
    Teacher: type[DeclarativeMeta]
    SubstituteTeacher: type[DeclarativeMeta]
    Paper: type[DeclarativeMeta]
    GradedPaper: type[DeclarativeMeta]
    Seminar: type[DeclarativeMeta]
    Lecture: type[DeclarativeMeta]
    Keyword: type[DeclarativeMeta]


@pytest.fixture
def models(Base: type[DeclarativeBase]) -> Models:
    # models adapted from https://github.com/wtforms/wtforms-sqlalchemy/blob/master/tests/tests.py
    student_course = sa.Table(
        "student_course",
        Base.metadata,  # type: ignore[attr-defined]
        sa.Column("student_id", sa.Integer, sa.ForeignKey("student.id")),
        sa.Column("course_id", sa.Integer, sa.ForeignKey("course.id")),
    )

    class Course(Base):
        __tablename__ = "course"
        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
        name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
        # These are for better model form testing
        cost: Mapped[float] = mapped_column(sa.Numeric(5, 2), nullable=False)
        description: Mapped[str] = mapped_column(sa.Text, nullable=True)
        level: Mapped[CourseLevel] = mapped_column(sa.Enum("Primary", "Secondary"))
        level_with_enum_class: Mapped[CourseLevel] = mapped_column(sa.Enum(CourseLevel))
        has_prereqs: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
        started: Mapped[dt.datetime] = mapped_column(sa.DateTime, nullable=False)
        grade: Mapped[int] = mapped_column(AnotherInteger, nullable=False)
        transcription: Mapped[str] = mapped_column(AnotherText, nullable=False)

        @property
        def url(self):
            return f"/courses/{self.id}"

    class School(Base):
        __tablename__ = "school"
        id: Mapped[int] = mapped_column("school_id", sa.Integer, primary_key=True)
        name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

        student_ids = association_proxy(
            "students", "id", creator=lambda sid: Student(id=sid)
        )

        @property
        def url(self):
            return f"/schools/{self.id}"

    class Student(Base):
        __tablename__ = "student"
        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
        full_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, unique=True)
        dob: Mapped[dt.date | None] = mapped_column(sa.Date(), nullable=True)
        date_created: Mapped[dt.datetime] = mapped_column(
            sa.DateTime,
            default=lambda: dt.datetime.now(dt.timezone.utc),
            doc="date the student was created",
        )

        current_school_id: Mapped[int] = mapped_column(
            sa.Integer, sa.ForeignKey("school.school_id"), nullable=False
        )
        current_school: Mapped[School] = relationship(School, backref=backref("students"))
        possible_teachers = association_proxy("current_school", "teachers")

        courses: Mapped[list[Course]] = relationship(
            Course,
            secondary=student_course,
            backref=backref("students", lazy="dynamic"),
        )

        # Test complex column property
        course_count = column_property(
            sa.select(sa.func.count(student_course.c.course_id))
            .where(student_course.c.student_id == id)
            .scalar_subquery()
        )

        @property
        def url(self):
            return f"/students/{self.id}"

    class Teacher(Base):
        __tablename__ = "teacher"
        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)

        full_name: Mapped[str] = mapped_column(
            sa.String(255), nullable=False, unique=True, default="Mr. Noname"
        )

        current_school_id: Mapped[int | None] = mapped_column(
            sa.Integer, sa.ForeignKey("school.school_id"), nullable=True
        )
        current_school: Mapped[School | None] = relationship(School, backref=backref("teachers"))
        curr_school_id = synonym("current_school_id")

        substitute: Mapped[SubstituteTeacher | None] = relationship("SubstituteTeacher", uselist=False, backref="teacher")

        data = sa.Column(sa.PickleType)

        @property
        def fname(self):
            return self.full_name

    class SubstituteTeacher(Base):
        __tablename__ = "substituteteacher"
        id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("teacher.id"), primary_key=True)

    class Paper(Base):
        __tablename__ = "paper"

        satype = sa.Column(sa.String(50))
        __mapper_args__ = {"polymorphic_identity": "paper", "polymorphic_on": satype}

        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
        name: Mapped[str] = mapped_column(sa.String, nullable=False, unique=True)

    class GradedPaper(Paper):
        __tablename__ = "gradedpaper"

        __mapper_args__ = {"polymorphic_identity": "gradedpaper"}

        id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("paper.id"), primary_key=True)

        marks_available: Mapped[int] = mapped_column(sa.Integer)

    class Seminar(Base):
        __tablename__ = "seminar"

        title: Mapped[str] = mapped_column(sa.String, primary_key=True)
        semester: Mapped[str] = mapped_column(sa.String, primary_key=True)

        label = column_property(title + ": " + semester)

    lecturekeywords_table = sa.Table(
        "lecturekeywords",
        Base.metadata,  # type: ignore[attr-defined]
        sa.Column("keyword_id", sa.Integer, sa.ForeignKey("keyword.id")),
        sa.Column("lecture_id", sa.Integer, sa.ForeignKey("lecture.id")),
    )

    class Keyword(Base):
        __tablename__ = "keyword"

        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
        keyword: Mapped[str] = mapped_column(sa.String)

    class Lecture(Base):
        __tablename__ = "lecture"
        __table_args__ = (
            sa.ForeignKeyConstraint(
                ["seminar_title", "seminar_semester"],
                ["seminar.title", "seminar.semester"],
            ),
        )

        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
        topic: Mapped[str] = mapped_column(sa.String)
        seminar_title: Mapped[str] = mapped_column(sa.String, sa.ForeignKey(Seminar.title))
        seminar_semester: Mapped[str] = mapped_column(sa.String, sa.ForeignKey(Seminar.semester))
        seminar: Mapped[Seminar] = relationship(
            Seminar, foreign_keys=[seminar_title, seminar_semester], backref="lectures"
        )
        kw: Mapped[list[Keyword]] = relationship("Keyword", secondary=lecturekeywords_table)
        keywords = association_proxy(
            "kw", "keyword", creator=lambda kw: Keyword(keyword=kw)
        )

    return Models(
        Course=Course,
        School=School,
        Student=Student,
        Teacher=Teacher,
        SubstituteTeacher=SubstituteTeacher,
        Paper=Paper,
        GradedPaper=GradedPaper,
        Seminar=Seminar,
        Lecture=Lecture,
        Keyword=Keyword,
    )
