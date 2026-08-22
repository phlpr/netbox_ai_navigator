class NavigatorError(Exception):
    """Base class for controlled assistant errors."""


class InvalidRequestError(NavigatorError):
    pass


class ProviderError(NavigatorError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class UngroundedResponseError(ProviderError):
    """The provider response contains NetBox data that cannot be traced to tool results."""


class AgentLimitError(NavigatorError):
    pass


class ToolError(NavigatorError):
    pass


class ToolNotFoundError(ToolError):
    pass


class ToolValidationError(ToolError):
    pass
