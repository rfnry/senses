class SensesException(Exception):
    pass


class ConfigurationError(SensesException):
    pass


class InputError(SensesException):
    pass


class NotFoundError(SensesException):
    pass


class ModelError(SensesException):
    pass


class TransportError(SensesException):
    pass
