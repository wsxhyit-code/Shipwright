# 订单接口约定

GET /api/orders/summary?customer_id=<整数>

合法客户即使没有订单，也返回 HTTP 200：
`{"count": 0, "total_cents": 0, "average_cents": null}`。

存在订单时 count 是订单数，total_cents 是金额之和，average_cents 是算术平均值。
金额以整数分存储；返回平均值可以是 JSON 数字。零金额订单不能当作没有订单。
非法 customer_id 返回 HTTP 400。数据库或配送依赖不可用返回 HTTP 503。

`/health` 仅检查进程存活，不证明数据库、依赖与所有业务逻辑正常。
