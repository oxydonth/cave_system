"""Shared fixtures for the whole test suite.

Keep this file free of Home Assistant: it is loaded for every test, including the ones that
must run without it (the Python 3.11 tests of the planned ops/ package, and the
Home Assistant-free unit tests). Home Assistant's fixtures are set up in the conftest.py of
each directory that needs them, for example tests/cave_heating/conftest.py.
"""
