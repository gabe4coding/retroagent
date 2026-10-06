import sys


def test_package_imports():
    import kb

    assert kb.__version__ == "0.1.0"


def test_runtime_is_python_39_or_newer():
    assert sys.version_info >= (3, 9)
