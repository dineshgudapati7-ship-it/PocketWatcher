from pocketwatcher.db.session import CONNECT_TIMEOUT_SECONDS, connect_args


def test_postgres_connections_time_out_instead_of_hanging():
    assert connect_args("postgresql+psycopg://u:p@db:5432/pw") == {"connect_timeout": CONNECT_TIMEOUT_SECONDS}


def test_sqlite_gets_no_postgres_only_arguments():
    assert connect_args("sqlite:///pocketwatcher.db") == {}
