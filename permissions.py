"""权限模型：角色 → 可访问的内容层。

内容分两层：
  all      全员层（报销制度、发票要求、FAQ 等）
  finance  财务专属层（科目口径、月结手册、准则口径等）

角色：
  employee  普通员工 → 只看 all
  finance   财务      → 看 all + finance
  admin     管理员    → 看 all + finance，且可管理账号

要更细粒度（如按部门/数据行），把这里换成你自己的角色-层映射，
并在入库时给文档打对应的 layer 标签即可。
"""


def allowed_layers(roles) -> list:
    roles = set(roles or [])
    if roles & {"finance", "admin"}:
        return ["all", "finance"]
    return ["all"]


def is_admin(roles) -> bool:
    return "admin" in set(roles or [])
