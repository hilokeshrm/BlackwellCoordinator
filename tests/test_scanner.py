from blackwell_mcp import scanner


def test_detects_entry_config_and_framework(raw_finetune):
    r = scanner.scan(raw_finetune)
    assert r.entry_command == "python train.py --config config.json"
    assert r.total_epochs == 20 and r.config_file == "config.json" and not r.sdk_integrated


def test_uses_project_virtualenv(raw_finetune):
    exe = raw_finetune / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    cmd = scanner.scan(raw_finetune).entry_command
    assert str(exe) in cmd and cmd.endswith("train.py --config config.json")


def test_huggingface_detection(tmp_path):
    (tmp_path / "finetune.py").write_text("from transformers import Trainer\nif __name__ == '__main__':\n    Trainer()\n")
    r = scanner.scan(tmp_path)
    assert r.framework == "huggingface" and r.entry_file == "finetune.py" and r.has_main_guard
