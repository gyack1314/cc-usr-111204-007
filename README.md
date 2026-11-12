# 储氢地质测井LAS资料的曲线索引采集

此Python项目管理独立的能源领域合成源资料，`data/`为原始样本及访问配置，`source_data.io`负责严格读取和字节身份，`source_data.domain`提供现有库存摘要，`source_data.replay`按声明顺序返回不解释的源响应。`source_data.las`流式解析LAS 2.0的Version/Well/Curve/Parameter/ASCII段，`source_data.index`在sqlite磁盘索引上完成曲线采集、标准深度换算与窗口随机访问。字段和支持的来源口径见[资料约定](docs/data-contract.md)。所有机构、设备、对象与数值均为虚构。

Python 3.11及以上即可使用，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
python -m source_data.cli index data/normal.json build/demo.db
python -m source_data.cli query build/demo.db --data-root data \
  --runs down-feet up-metres --lo 30.48 --hi 30.7848
python -m source_data.cli sequential build/demo.db --data-root data --run up-metres
```

`inspect`展示已有的原始库存，不修改资料。`index`按`runs`/`curve_map`/`unit_aliases`构建磁盘索引并输出构建报告（含已完成前缀、失败位置、不完整尾与头部诊断）；WRAP=YES按曲线数跨物理行重建测点，重测记录与NULL缺测原样保留，不平均、不插值。`query`读取闭合标准深度窗，正反向文件统一按`(标准深度,run_id,原记录序号)`排序，查询前重算SHA256，源文件变更即拒绝。`sequential`完整顺序抽取某run，可与窗口结果按字节区间核对来源。`invalid-`前缀文件为不合法输入，其他变体保留不同业务边界；源协议与二进制格式使用资料约定中的有限范围，未解释部分仍以原字节保存。

固定基础镜像见Dockerfile，可执行`docker build -t energy-source .`，再用`docker run --rm energy-source`显示摘要；`docker run --rm energy-source python -m unittest discover -s tests -v`执行已有检查。资料和回放不依赖真实设备、账户或实时网络服务。
