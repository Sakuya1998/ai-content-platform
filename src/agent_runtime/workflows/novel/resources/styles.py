"""Builtin style definitions and frozen workspace style rendering."""

STYLE_PROFILES: dict[str, dict] = {
    "jin_yong": {
        "name": "金庸风格",
        "syntax": ["使用流畅的汉语语法", "多用逗号连接的复合句", "段落长短相间"],
        "sentence_length": "中等偏长,平均30-50字",
        "vocabulary": ["四字成语频繁使用", "书面语为主", "古典武侠术语"],
        "narrative_techniques": ["全知视角", "伏笔", "侧面烘托", "对比"],
        "pacing": "张弛有度,大场面与抒情交替",
        "examples": [
            "那少年长剑一挥,剑光如匹练般横贯长空,恍若银河倒泻。",
            "他心中一凛,暗想:此人内力之深,实已臻化境,我万万不是对手。",
            "夕阳西下,余晖洒满山谷,映得漫山红叶如火如荼,煞是壮观。",
        ],
    },
    "gu_long": {
        "name": "古龙风格",
        "syntax": ["短句为主", "大量换行", "少用复合句"],
        "sentence_length": "短促有力,平均10-20字",
        "vocabulary": ["简洁直白", "口语化", "偶尔诗化表达"],
        "narrative_techniques": ["悬念", "转折", "留白"],
        "pacing": "快节奏,持续紧张",
        "examples": [
            "刀很快。刀光一闪。人已倒下。",
            "夜很冷。风很冷。他的心更冷。",
            "天涯远不远?不远。人就在天涯,天涯怎么会远?",
        ],
    },
    "murakami": {
        "name": "村上春树风格",
        "syntax": ["使用流畅的日语式翻译腔", "大量比喻", "独特的观察视角"],
        "sentence_length": "中等,平均20-40字",
        "vocabulary": ["日常词汇", "音乐/文学/食品专有名词", "独特的比喻"],
        "narrative_techniques": ["第一人称", "超现实元素", "细致的日常描写"],
        "pacing": "舒缓,注重氛围和心理",
        "examples": [
            "如同置身于深海般的孤独感,将我轻轻包裹。",
            "她像秋日的午后一样,安静而透明。",
            "不可思议的是,那旋律一旦响起,我便再也无法逃避自己的记忆。",
        ],
    },
    "yu_hua": {
        "name": "余华风格",
        "syntax": ["朴素直接的语法", "少修辞", "多用短句和白描"],
        "sentence_length": "简短,平均10-25字",
        "vocabulary": ["朴素", "民间词汇", "直白有力"],
        "narrative_techniques": ["零度叙事", "黑色幽默", "残酷现实"],
        "pacing": "冷静克制,暗流涌动",
        "examples": [
            "他活着,像一头牲口那样活着。",
            "那年冬天,我爷爷死了,死得很平静。",
            "月光照在路上,像是撒满了盐。",
        ],
    },
}


def get_style_prompt(style_name: str) -> str:
    """将风格档案转换为可嵌入 Prompt 的文本块。

    Args:
        style_name: 风格标识(STYLE_PROFILES 的 key)

    Returns:
        拼接好的风格描述文本;未知风格回退到默认风格并给出提示行。
    """
    from agent_runtime.workflows.novel.context import current_novel

    dependencies = current_novel()
    snapshot = dependencies.style_snapshot if dependencies else None
    if snapshot and snapshot.get("payload"):
        from agent_runtime.workflows.novel.resources.catalog import normalize_style_payload

        profile = {"name": snapshot.get("name", "Custom"), **normalize_style_payload(snapshot["payload"])}
    else:
        profile = STYLE_PROFILES.get(style_name)
    if profile is None:
        profile = STYLE_PROFILES["jin_yong"]
        header = f"# 风格: {profile['name']}(注意:未知风格 {style_name!r},已回退到默认)"
    else:
        header = f"# 风格: {profile['name']}"

    lines = [
        header,
        f"## 语法规则: {'; '.join(profile['syntax'])}",
        f"## 句子长度: {profile['sentence_length']}",
        f"## 词汇选择: {'; '.join(profile['vocabulary'])}",
        f"## 叙事技巧: {'; '.join(profile['narrative_techniques'])}",
        f"## 节奏: {profile['pacing']}",
        "## 风格示例:",
    ]
    lines += [f"- {e}" for e in profile["examples"]]
    return "\n".join(lines)
