from pathlib import Path
RULE_VERSION = "2026.11.19.1"
TITLE = '再生合金成分混配与验收'
SHORT = 'alloy-blend'
MODULES = ['来料接收', '成分归一', '配方约束', '混配试算', '不确定区间', '验收对照', '复配批次', '连炉排产']
HEADERS = ['lot', 'batch', 'mass', 'unit', 'moisture', 'element', 'assay', 'basis', 'revision']
# 连炉排产的可选列；缺少时该炉次不会被排入连炉方案。
OPTIONAL_HEADERS = ['furnace', 'seq', 'grade']
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "operations.csv"
SEQUENCE_SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "furnace_sequence.csv"
LIMIT = 10000
