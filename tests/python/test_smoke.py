def test_package_imports_and_exposes_version():
    import shadowfill

    assert isinstance(shadowfill.__version__, str)
    assert shadowfill.__version__.count(".") >= 2
