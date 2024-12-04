from typing import Any


class ValueRef:
    """
    Minimal class to hold a reference to a scalar value, allowing user to get outputs
    from scalar inout/out arguments.
    """

    def __init__(self, init_value: Any):
        super().__init__()
        self.value = init_value

    @property
    def slangpy_signature(self) -> str:
        return f"[{type(self.value).__name__}]"


def intRef(init_value: int = 0) -> ValueRef:
    return ValueRef(int(init_value))


def floatRef(init_value: float = 0.0) -> ValueRef:
    return ValueRef(float(init_value))
