"""TensorBoard event 文件解析 → Loss/LR 曲线数据

无 tfevents 时（训练镜像缺 tensorboard、report_to 降级为 none）兜底解析
checkpoint-*/trainer_state.json 的 log_history（HF Trainer 每次存 checkpoint 时
全量重写），输出与 tfevents 路径相同的结构 {"loss": [[step, value], ...], ...}。
"""
import json
import os
import glob


def read_metrics(tb_dir: str, limit: int = 2000) -> dict:
    """返回 {"loss": [[step, value], ...], "learning_rate": [...], "epoch": [...]}"""
    result: dict[str, list] = {}
    if not tb_dir or not os.path.isdir(tb_dir):
        return result
    files = glob.glob(os.path.join(tb_dir, "**", "events.out.tfevents*"), recursive=True)
    if files:
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
            result = {}
    # 兜底：tfevents 缺失/解析失败时从 checkpoint 的 trainer_state.json 取曲线
    if not result.get("loss"):
        ckpt_dir = os.path.join(os.path.dirname(tb_dir.rstrip(os.sep)), "ckpt")
        result.update(_from_trainer_state(ckpt_dir, limit))
    return {k: v[-limit:] for k, v in result.items()}


def _ckpt_step(path: str) -> int:
    try:
        return int(path.rsplit("-", 1)[-1].split("/")[0])
    except ValueError:
        return -1


def _from_trainer_state(ckpt_dir: str, limit: int) -> dict:
    """取步数最大的 checkpoint 的 trainer_state.json（log_history 为全量历史）"""
    out: dict[str, list] = {}
    files = sorted(glob.glob(os.path.join(ckpt_dir, "checkpoint-*", "trainer_state.json")),
                   key=_ckpt_step)
    if not files:
        return out
    try:
        with open(files[-1], encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return out
    for e in st.get("log_history", []):
        step = e.get("step")
        if step is None:
            continue
        if "loss" in e:
            out.setdefault("loss", []).append([step, round(float(e["loss"]), 6)])
        if "learning_rate" in e:
            out.setdefault("learning_rate", []).append([step, round(float(e["learning_rate"]), 6)])
        if "epoch" in e:
            out.setdefault("epoch", []).append([step, round(float(e["epoch"]), 6)])
    return out
