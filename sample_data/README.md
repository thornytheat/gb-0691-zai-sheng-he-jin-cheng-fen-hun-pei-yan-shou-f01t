样例包含重复、同键冲突、空值和单位/格式变体。operations.csv为UTF-8 BOM，legacy-gb18030.csv为同内容旧编码。

furnace_sequence.csv 供第 8 模块「连炉排产」使用，在九列之外增加可选的 furnace、seq、grade：

- F-01：CU110(1000kg) → CU70(200kg) 需清洗（铜高残料）→ CU90 连浇 → CU70 因本炉铜高停等 → CU110 等待续跑。
- F-02：CU70 → CU90 → CU110，顺序升高，全部可连浇。
- F-03：CU90 → CU110 需清洗（上炉残料抬高出炉铜）→ CU70 连浇。
- H-900 三列排产字段全空，不进入连炉方案。
