"""Safe errors; dependency exception text never crosses the public boundary."""


class MarketplaceError(Exception):
    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(code)
