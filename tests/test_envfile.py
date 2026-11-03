import os

from bian.envfile import load_env_file


def test_load_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# 注释\nexport A_KEY="x y"\nB_KEY=pa#ss=word\nC_KEY=\nD_KEY=keep\n\nnot a line\n', encoding="utf-8")
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("D_KEY", "from-env")
    loaded = load_env_file(str(f))
    assert os.environ["A_KEY"] == "x y"
    assert os.environ["B_KEY"] == "pa#ss=word"  # # 和 = 都保留
    assert "C_KEY" not in os.environ  # 空值不设置
    assert os.environ["D_KEY"] == "from-env"  # 不覆盖已有环境变量
    assert sorted(loaded) == ["A_KEY", "B_KEY"]
    for k in loaded:
        monkeypatch.delenv(k)


def test_missing_file_is_noop(tmp_path):
    assert load_env_file(str(tmp_path / "nope")) == []
