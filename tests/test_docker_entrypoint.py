from docker_entrypoint import runtime_db_path


def test_runtime_db_path_matches_bothost_persistent_layout():
    assert runtime_db_path("").as_posix() == "/app/data/mgn_vpn.sqlite3"
    assert runtime_db_path("mgn.sqlite3").as_posix() == "/app/data/mgn.sqlite3"
    assert runtime_db_path("/app/legacy.sqlite3").as_posix() == "/app/data/legacy.sqlite3"
    assert runtime_db_path("/app/data/current.sqlite3").as_posix() == "/app/data/current.sqlite3"
