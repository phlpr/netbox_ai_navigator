class NavigatorError(Exception):
    """Base class for controlled assistant errors."""


class InvalidRequestError(NavigatorError):
    pass


class ProviderError(NavigatorError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class AgentLimitError(NavigatorError):
    pass


class ToolError(NavigatorError):
    pass


class ToolNotFoundError(ToolError):
    pass


class ToolValidationError(ToolError):
    pass
