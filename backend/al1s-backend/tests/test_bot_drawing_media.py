import hashlib
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from al1s.bot_drawing.media import DrawingMediaStore


def picture():
    out = BytesIO()
    Image.new("RGB", (8, 8), "orange").save(out, "PNG")
    return out.getvalue()


def test_durable_owned_input_and_output_are_idempotent(tmp_path):
    store = DrawingMediaStore(tmp_path)
    task = uuid4()
    body = picture()
    reference, digest = store.put(task, "input", body)
    assert reference == f"al1s-drawing/{task}/input.png"
    assert digest == hashlib.sha256(body).hexdigest()
    assert store.put(task, "input", body) == (reference, digest)
    assert store.read(reference, expected_sha256=digest) == body
    other = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(other, "PNG")
    with pytest.raises(ValueError, match="drawing_media_conflict"):
        store.put(task, "input", other.getvalue())
    jpeg = BytesIO()
    Image.new("RGB", (8, 8), "orange").save(jpeg, "JPEG")
    with pytest.raises(ValueError, match="drawing_media_conflict"):
        store.put(task, "input", jpeg.getvalue())
    output, _ = store.put(task, "output", body)
    assert output == f"al1s-drawing/{task}/output.png"


def test_paths_and_symlinks_cannot_escape_owned_task_directory(tmp_path):
    store = DrawingMediaStore(tmp_path)
    task = uuid4()
    with pytest.raises(ValueError, match="drawing_media_reference_invalid"):
        store.read(f"al1s-drawing/{task}/../../private", expected_sha256="a" * 64)
    root = tmp_path / "al1s-drawing"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    try:
        (root / str(task)).symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="drawing_media_path_invalid"):
        store.put(task, "input", picture())
    assert list(target.iterdir()) == []


def test_changed_file_is_not_accepted_for_send_or_cleanup(tmp_path):
    store = DrawingMediaStore(tmp_path)
    task = uuid4()
    reference, digest = store.put(task, "output", picture())
    (tmp_path / Path(reference)).write_bytes(b"different")
    with pytest.raises(ValueError, match="drawing_media_changed"):
        store.read(reference, expected_sha256=digest)
    with pytest.raises(ValueError, match="drawing_media_changed"):
        store.delete(reference, expected_sha256=digest)


def test_output_gif_keeps_all_frames_and_is_deleted_as_owned_media(tmp_path):
    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue", "green")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    body = output.getvalue()
    store, task = DrawingMediaStore(tmp_path), uuid4()
    reference, digest = store.put(task, "output", body)
    assert reference == f"al1s-drawing/{task}/output.gif"
    assert store.read(reference, expected_sha256=digest) == body
    with Image.open(BytesIO(body)) as image:
        assert image.n_frames == 3
    store.delete(reference, expected_sha256=digest)
    assert not (tmp_path / Path(reference)).exists()
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        store.put(uuid4(), "input", body)


def test_output_gif_rejects_static_and_too_many_frames(tmp_path):
    store = DrawingMediaStore(tmp_path)
    single = BytesIO()
    Image.new("RGB", (8, 8), "red").save(single, "GIF")
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        store.put(uuid4(), "output", single.getvalue())
    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), (index * 10, 0, 0)) for index in range(17)]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200)
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        store.put(uuid4(), "output", output.getvalue())
