from dedupe import ReviewStore


def test_marked_key_is_reviewed():
    store = ReviewStore(":memory:")

    assert not store.already_reviewed("k1")
    store.mark_reviewed("k1")
    assert store.already_reviewed("k1")


def test_marking_twice_is_harmless():
    store = ReviewStore(":memory:")

    store.mark_reviewed("k1")
    store.mark_reviewed("k1")

    assert store.already_reviewed("k1")


def test_survives_across_instances(tmp_path):
    db = str(tmp_path / "reviewed.db")

    first = ReviewStore(db)
    first.mark_reviewed("owner/repo@sha1")

    second = ReviewStore(db)  # fresh instance = simulated restart
    assert second.already_reviewed("owner/repo@sha1")


def test_store_is_bounded():
    store = ReviewStore(":memory:", max_rows=2)

    store.mark_reviewed("k1")
    store.mark_reviewed("k2")
    store.mark_reviewed("k3")

    assert not store.already_reviewed("k1")
    assert store.already_reviewed("k2")
    assert store.already_reviewed("k3")


def test_no_db_file_created_until_used(tmp_path):
    db = tmp_path / "lazy.db"

    ReviewStore(str(db))
    assert not db.exists()

    ReviewStore(str(db)).mark_reviewed("k1")
    assert db.exists()
