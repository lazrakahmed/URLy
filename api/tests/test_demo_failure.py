def test_deliberate_failure():
    assert False, "demo: a failing test must stop the pipeline before any image is pushed"
