"""TensorBoard event 文件解析 → Loss/LR 曲线数据"""
import os
import glob


def read_metrics(tb_dir: str, limit: int = 2000) -> dict:
    """返回 {"loss": [[step, value], ...], "learning_rate": [...], "epoch": [...]}"""
    result: dict[str, list] = {}
    if not tb_dir or not os.path.isdir(tb_dir):
        return result
    files = glob.glob(os.path.join(tb_dir, "**", "events.out.tfevents*"), recursive=True)
    if not files:
        return result
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        ea = EventAccumulator(tb_dir, size_guidance={"scalars": 0}, use_tfrecord_data_loader=False)
        ea.Reload()
        for tag in ea.Tags().get("scalars", []):
            name = tag
            for alias, key in (("loss", "loss"), ("train_loss", "loss"), ("learning_rate", "learning_rate"),
                               ("lr", "learning_rate"), ("epoch", "epoch"), ("train/loss", "loss"),
                               ("train/learning_rate", "learning_rate")):
                if tag == alias or tag.endswith(f"/{alias}"):
                    name = key
                    break
            series = result.setdefault(name, [])
            for ev in ea.Scalars(tag):
                series.append([ev.step, round(ev.value, 6)])
    except Exception:
        return result
    return {k: v[-limit:] for k, v in result.items()}
