"""Stable errors raised by dataset and evaluation boundaries."""


class EvaluationError(Exception):
    """Base class for expected evaluation failures."""


class DatasetValidationError(EvaluationError):
    """The dataset is not valid or cannot be frozen."""


class EvaluationNotFoundError(EvaluationError):
    """A requested dataset, config or run does not exist."""

