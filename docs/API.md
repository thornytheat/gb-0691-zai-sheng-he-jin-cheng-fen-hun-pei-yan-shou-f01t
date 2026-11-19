# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m8`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/m8?run_id=`：连炉排产方案（按炉座/炉序排列，含每炉承接残料、清洗与停等依据、成分变化与来源行）。不带run_id时用样例运行。

`POST /api/m8/confirm`：确认一炉的下一步。字段 `run_id`、`furnace`、`heat_key`、`action`（开炉/连浇/清洗/停等），可选 `idempotency_key`。动作必须在当前可选集合内，否则409；同一幂等键重复提交返回既有事件（`reused:true`），不重复累计残料。

`POST /api/m8/resume`：停等后续跑。字段 `run_id`、`furnace`，可选 `heat_key`。保留原有炉序与炉内残料；重复提交同样幂等。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
