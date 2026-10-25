from backend.models.catalog import Candidate

# 固定候选池不写数据库；只有管理员确认的商品进入业务目录。
CATALOG = tuple(
    Candidate(
        id=f"{prefix}-{index:03d}",
        name=f"{brand} {model}",
        category=category,
        description=description,
        specifications=dict(specifications),
        suggested_price_cents=price,
    )
    for prefix, category, brand, description, variants in (
        (
            "GPU",
            "显卡",
            "星澜",
            "图形处理与游戏创作系列，提供多种显存容量和散热规格。",
            (
                ("G60 8G 双风扇显卡", 129900, (("显存", "8GB"), ("接口", "PCIe 4.0"))),
                ("G65 8G 静音显卡", 159900, (("显存", "8GB"), ("散热", "双风扇"))),
                ("G70 12G 游戏显卡", 249900, (("显存", "12GB"), ("散热", "三风扇"))),
                ("G75 16G 游戏显卡", 329900, (("显存", "16GB"), ("接口", "PCIe 4.0"))),
                ("G80 16G 创作显卡", 429900, (("显存", "16GB"), ("定位", "图形创作"))),
                ("G85 20G 创作显卡", 529900, (("显存", "20GB"), ("散热", "三风扇"))),
                ("G90 24G 工作站显卡", 699900, (("显存", "24GB"), ("定位", "工作站"))),
                ("G50 6G 入门显卡", 89900, (("显存", "6GB"), ("散热", "单风扇"))),
            ),
        ),
        (
            "LAPTOP",
            "笔记本电脑",
            "云序",
            "适合办公与学习的笔记本电脑，多种内存与存储配置。",
            (
                ("Air14 轻薄电脑", 399900, (("内存", "16GB"), ("硬盘", "512GB SSD"))),
                ("Pro14 办公电脑", 499900, (("内存", "32GB"), ("硬盘", "1TB SSD"))),
                ("Book15 学习电脑", 329900, (("内存", "16GB"), ("屏幕", "15.6英寸"))),
                ("Studio16 创作电脑", 699900, (("内存", "32GB"), ("屏幕", "16英寸"))),
            ),
        ),
        (
            "KEYBOARD",
            "键盘",
            "方格",
            "机械键盘系列，涵盖紧凑配列与全尺寸布局。",
            (
                ("K68 无线机械键盘", 19900, (("配列", "68键"), ("连接", "无线"))),
                ("K87 有线机械键盘", 15900, (("配列", "87键"), ("连接", "USB"))),
                ("K98 三模机械键盘", 29900, (("配列", "98键"), ("连接", "三模"))),
                ("K104 全尺寸键盘", 12900, (("配列", "104键"), ("连接", "USB"))),
            ),
        ),
        (
            "HEADSET",
            "耳机",
            "回声",
            "音频系列，覆盖日常通勤、运动与游戏场景。",
            (
                ("H10 蓝牙耳机", 9900, (("佩戴", "入耳式"), ("连接", "蓝牙"))),
                ("H20 降噪耳机", 29900, (("佩戴", "头戴式"), ("功能", "主动降噪"))),
                ("H30 游戏耳机", 19900, (("连接", "USB"), ("功能", "麦克风"))),
                ("H40 运动耳机", 15900, (("佩戴", "挂耳式"), ("连接", "蓝牙"))),
            ),
        ),
        (
            "MONITOR",
            "显示器",
            "光域",
            "桌面显示器系列，提供办公、游戏和图形创作配置。",
            (
                ("M24 办公显示器", 69900, (("尺寸", "24英寸"), ("分辨率", "1080P"))),
                ("M27 高清显示器", 129900, (("尺寸", "27英寸"), ("分辨率", "2K"))),
                ("M27 游戏显示器", 159900, (("刷新率", "165Hz"), ("分辨率", "2K"))),
                ("M32 创作显示器", 249900, (("尺寸", "32英寸"), ("分辨率", "4K"))),
            ),
        ),
    )
    for index, (model, price, specifications) in enumerate(variants, start=1)
)


def search_candidates(query: str, category: str | None) -> list[Candidate]:
    terms = query.casefold().split()
    return [
        item
        for item in CATALOG
        if (not category or item.category == category)
        and all(
            term
            in " ".join(
                (item.name, item.category, item.description, *item.specifications.values())
            ).casefold()
            for term in terms
        )
    ]
