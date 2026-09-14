#!/usr/bin/env python3
# 验收资料生成器（按用户提供的 6 套正式模板）
# 由 web_app/server.py 通过子进程调用：
#   python3 acceptance_docx.py --package <in.json> <out.zip>
# in.json 结构：{"items":[...作业记录...], "meta":{...项目信息...}}
# 作业记录字段见 server._acceptance_jobs / acceptance_build.acceptance_jobs 的输出。
#
# 产出：一个 ZIP，内含 6 份 Word 表单（与用户模板版式一致）+ 字段说明.txt
#   - 线路走廊清理项目现场工程量签证单.docx   （核心：逐电杆/地点明细）
#   - 工程量明细表.docx                      （按树种汇总的工程量 BOQ）
#   - 现场工程量签证表.docx                  （逐作业、含签证叙述）
#   - 工程量签证表.docx                      （汇总签证）
#   - 日常维修包项目申请表.docx              （配网适用申请表）
#   - 附录H施工前后照片.docx                （修剪前/后照片页）
import os, sys, json, re, io, tempfile, zipfile, datetime
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

CN = "宋体"
HEI = "黑体"
LEFT = WD_ALIGN_PARAGRAPH.LEFT
CENTER = WD_ALIGN_PARAGRAPH.CENTER


# ---------------- 基础工具 ----------------
def set_cn(run, name=CN, size=10.5, bold=False):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn('w:rFonts'))
    if rfonts is None:
        rfonts = OxmlElement('w:rFonts')
        rpr.append(rfonts)
    rfonts.set(qn('w:eastAsia'), name)
    rfonts.set(qn('w:ascii'), name)
    rfonts.set(qn('w:hAnsi'), name)


def set_cell_text(cell, text, *, bold=False, name=CN, size=10.5, align=LEFT):
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = align
    for i, line in enumerate(str(text).split("\n")):
        if i > 0:
            p.add_run().add_break()
        run = p.add_run(line)
        set_cn(run, name=name, size=size, bold=bold)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def set_table_borders(table):
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        e = OxmlElement('w:' + edge)
        e.set(qn('w:val'), 'single')
        e.set(qn('w:sz'), '4')
        e.set(qn('w:space'), '0')
        e.set(qn('w:color'), '000000')
        borders.append(e)
    tblPr.append(borders)


def mcell(t, r, c1, c2, text, **kw):
    """合并第 r 行 c1..c2 列并写入文本。"""
    cell = t.cell(r, c1)
    if c2 > c1:
        cell = cell.merge(t.cell(r, c2))
    set_cell_text(cell, text, **kw)
    return cell


def add_title(doc, text, size=16):
    p = doc.add_paragraph()
    p.alignment = CENTER
    run = p.add_run(text)
    set_cn(run, name=HEI, size=size, bold=True)
    return p


def add_para(doc, text, *, name=CN, size=10.5, bold=False, align=LEFT):
    p = doc.add_paragraph()
    p.alignment = align
    run = p.add_run(text)
    set_cn(run, name=name, size=size, bold=bold)
    return p


def set_col_widths(t, widths):
    for i, w in enumerate(widths):
        if w:
            t.columns[i].width = Cm(w)


def _setup_margins(doc):
    for s in doc.sections:
        s.top_margin = Cm(2.0); s.bottom_margin = Cm(2.0)
        s.left_margin = Cm(2.0); s.right_margin = Cm(2.0)


# ---------------- 上下文派生 ----------------
def derive_ctx(items, meta):
    meta = meta or {}
    ctx = {
        "project": meta.get("project") or "树障清理修剪工程",
        "project_no": meta.get("project_no") or "",
        "contractor": meta.get("contractor") or "",
        "manager": meta.get("manager") or "",
        "date_start": meta.get("date_start", ""),
        "date_end": meta.get("date_end", ""),
        "city": meta.get("city") or "",
        "bureau": meta.get("bureau") or "",
        "station": meta.get("station") or "",
    }
    ds, de = ctx["date_start"], ctx["date_end"]
    ctx["period"] = (ds + " 至 " + de) if (ds or de) else ""
    area = (items[0].get("pole_area") or "") if items else ""
    parts = [p for p in area.split("/") if p]
    if not ctx["city"]:
        ctx["city"] = parts[0] if len(parts) > 0 else ""
    if not ctx["bureau"]:
        ctx["bureau"] = parts[1] if len(parts) > 1 else ""
    if not ctx["station"]:
        ctx["station"] = parts[-1] if parts else ""
    return ctx


def _loc(it):
    loc = it.get("pole_no", "") or ""
    d = it.get("pole_desc", "") or ""
    if d:
        loc += "（" + d + "）"
    return loc or "—"


def _remark(it):
    r = []
    if it.get("risk"):
        r.append("隐患等级：" + it["risk"])
    if it.get("branch"):
        r.append("剪下树枝量：" + it["branch"])
    if it.get("sign"):
        r.append("标示牌：" + it["sign"])
    if it.get("note"):
        r.append(it["note"])
    return "\n".join(r) or "—"


def _default_fetch(ftok):
    """子进程场景下按 file_token 取飞书附件字节；失败返回 None。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import server
        return server.fetch_attachment(ftok)
    except Exception:
        return None


# ---------------- 表单 1：线路走廊清理项目现场工程量签证单 ----------------
def build_visa_line_corridor(doc, items, ctx):
    add_title(doc, "线路走廊清理项目现场工程量签证单")
    add_para(doc, "项目名称：" + ctx["project"])
    add_para(doc, "项目编号：" + (ctx["project_no"] or "________________"))
    add_para(doc, "实施时间：" + (ctx["period"] or "________________"))
    doc.add_paragraph("")

    n = len(items)
    t = doc.add_table(rows=n + 3, cols=10)
    set_table_borders(t)
    hdr = ["序号", "实施地点", "项目内容", "树障种类", "规格", "单位",
           "施工作业计划编号", "", "工程量", "施工内容是否含（截干、清理场地、清运）"]
    for i, h in enumerate(hdr):
        set_cell_text(t.rows[0].cells[i], h, bold=True, name=HEI, size=9, align=CENTER)
    mcell(t, 0, 6, 7, "施工作业计划编号", bold=True, name=HEI, size=9, align=CENTER)

    for k, it in enumerate(items, 1):
        r = t.rows[k]
        set_cell_text(r.cells[0], k, align=CENTER)
        set_cell_text(r.cells[1], _loc(it), align=CENTER)
        set_cell_text(r.cells[2], "树障修剪", align=CENTER)
        set_cell_text(r.cells[3], it.get("tree", ""), align=CENTER)
        set_cell_text(r.cells[4], it.get("spec", "—"), align=CENTER)
        set_cell_text(r.cells[5], "棵", align=CENTER)
        set_cell_text(r.cells[6], it.get("ticket", ""), align=CENTER)
        set_cell_text(r.cells[8], "1", align=CENTER)
        set_cell_text(r.cells[9], "是", align=CENTER)

    notes = ("注：1.若为市政绿化树，签明树障胸径规格，结算套用广东省园林绿化工程综合定额(2018);\n"
             "     2.每亩费用标准适用于乔木每亩密度大于100棵的情况。\n"
             "     3.每处不足一亩的按照棵/丛计算，超过一亩的以亩计算。")
    mcell(t, n + 1, 0, 9, notes, size=8.5, align=LEFT)
    mcell(t, n + 2, 0, 2, "施工单位：\n负责人：\n日期：", size=9, align=LEFT)
    mcell(t, n + 2, 3, 6, "监理单位：\n负责人：\n日期：", size=9, align=LEFT)
    mcell(t, n + 2, 7, 9, "建设单位单位：\n负责人：\n日期：", size=9, align=LEFT)
    set_col_widths(t, [1.0, 3.0, 1.5, 1.5, 1.5, 1.0, 2.0, 2.0, 1.0, 2.0])


# ---------------- 表单 2：工程量明细表（按树种汇总） ----------------
def build_detail_table(doc, items, ctx):
    add_title(doc, "工程量明细表")
    add_para(doc, "项目名称：" + ctx["project"])
    add_para(doc, "项目编号：" + (ctx["project_no"] or "________________"))
    doc.add_paragraph("")

    # 按树种汇总
    groups = {}
    for it in items:
        key = it.get("tree", "未注明树种")
        g = groups.setdefault(key, {"cnt": 0, "sample": _loc(it)})
        g["cnt"] += 1
    glist = list(groups.items())

    t = doc.add_table(rows=len(glist) + 2, cols=8)
    set_table_borders(t)
    hdr = ["序号", "项目名称", "", "型号规格", "单位", "数量", "", "备注"]
    for i, h in enumerate(hdr):
        set_cell_text(t.rows[0].cells[i], h, bold=True, name=HEI, size=9, align=CENTER)
    mcell(t, 0, 1, 2, "项目名称", bold=True, name=HEI, size=9, align=CENTER)
    mcell(t, 0, 5, 6, "数量", bold=True, name=HEI, size=9, align=CENTER)

    for k, (tree, g) in enumerate(glist, 1):
        r = t.rows[k]
        set_cell_text(r.cells[0], k, align=CENTER)
        mcell(t, k, 1, 2, "树障修剪（%s）" % tree, align=CENTER)
        set_cell_text(r.cells[3], "", align=CENTER)
        set_cell_text(r.cells[4], "处", align=CENTER)
        mcell(t, k, 5, 6, str(g["cnt"]), align=CENTER)
        set_cell_text(r.cells[7], "示例：" + g["sample"], size=9)
    sig = len(glist) + 1
    mcell(t, sig, 0, 1, "施工单位：\n（章）\n项目经理：\n日期：", size=9, align=LEFT)
    mcell(t, sig, 2, 4, "监理单位：\n（章）\n代表：\n日期：", size=9, align=LEFT)
    mcell(t, sig, 5, 7, "项目实施单位：\n（章）\n代表：\n日期：", size=9, align=LEFT)
    set_col_widths(t, [1.0, 3.0, 1.0, 1.8, 1.2, 1.5, 1.5, 3.5])


# ---------------- 表单 3：现场工程量签证表 ----------------
def build_site_visa(doc, items, ctx):
    add_para(doc, "附表：", name=HEI, size=12, bold=True)
    add_title(doc, "现场工程量签证表", size=15)
    add_para(doc, "本表(含附件)一式三份，由施工单位填报，建设单位、项目实施单位、施工单位各存一份。", size=9)
    add_para(doc, "对应每一张工作票任务需出具一份现场工程量签证。完工5个工作日内需完成现场工程量签证。", size=9)
    doc.add_paragraph("")

    n = len(items)
    t = doc.add_table(rows=n + 3, cols=6)
    set_table_borders(t)
    mcell(t, 0, 0, 5, "项目名称：" + ctx["project"], bold=True, size=10.5, align=LEFT)
    narrative = ("致：%s：\n\n   本公司负责施工的%s工程现已施工完毕，经过三级自检，"
                 "工程质量符合国家及电力行业验收标准、技术规范和施工合同要求，"
                 "具体工作内容如下，请签证确认。\n   附件：\n□现场图片（必要时）。\n\n"
                 "                                             施工单位（章）：\n"
                 "                                                      代表：\n"
                 "                                                      日期：") % (ctx["station"] or "________供电所", ctx["project"])
    mcell(t, 1, 0, 5, narrative, size=9.5, align=LEFT)
    mcell(t, 2, 0, 1, "项目明细", bold=True, name=HEI, size=9, align=CENTER)
    set_cell_text(t.rows[2].cells[2], "单位", bold=True, name=HEI, size=9, align=CENTER)
    mcell(t, 2, 3, 4, "数量", bold=True, name=HEI, size=9, align=CENTER)
    set_cell_text(t.rows[2].cells[5], "说明", bold=True, name=HEI, size=9, align=CENTER)
    for k, it in enumerate(items, 1):
        r = t.rows[k + 2]
        mcell(t, k + 2, 0, 1, "树障修剪（%s）" % (it.get("tree", "") or "—"), align=CENTER)
        set_cell_text(r.cells[2], "棵", align=CENTER)
        mcell(t, k + 2, 3, 4, "1", align=CENTER)
        set_cell_text(r.cells[5], _remark(it), size=8.5)
    mcell(t, n + 2, 0, 2, "监理单位意见：\n\n监理单位（章）\n代表：\n日期：", size=9, align=LEFT)
    mcell(t, n + 2, 3, 5, "项目实施单位意见：\n\n项目实施单位（章）\n代表：\n日期：", size=9, align=LEFT)
    set_col_widths(t, [2.5, 2.5, 1.2, 2.0, 2.0, 4.0])


# ---------------- 表单 4：工程量签证表 ----------------
def build_quantity_visa(doc, items, ctx):
    add_title(doc, "工程量签证表")
    doc.add_paragraph("")
    t = doc.add_table(rows=4, cols=6)
    set_table_borders(t)
    mcell(t, 0, 0, 1, "项目名称：" + ctx["project"], bold=True, size=10, align=LEFT)
    mcell(t, 0, 2, 3, "施工作业计划编号：" + (items[0].get("ticket", "") if items else "________"), size=10, align=LEFT)
    mcell(t, 0, 4, 5, "", size=10, align=LEFT)
    narrative = ("%s（项目实施单位/监理单位）：\n\n   本公司负责施工的%s工程现已施工完毕，"
                 "经过三级自检，工程质量符合国家及电力行业验收标准、技术规范和施工合同的要求，"
                 "具体工作内容如下，请签证确认。\n\n   施工单位（盖章）：\n   项目负责人：\n"
                 "   日期：     年     月     日") % (ctx["station"] or "________", ctx["project"])
    mcell(t, 1, 0, 5, narrative, size=9.5, align=LEFT)
    mcell(t, 2, 0, 1, "签证内容", bold=True, name=HEI, size=9, align=CENTER)
    content = "本次施工主要内容：树障修剪（线路走廊清理），共 %d 处，详见《线路走廊清理项目现场工程量签证单》及附件照片。" % len(items)
    mcell(t, 2, 2, 5, content, size=9.5, align=LEFT)
    set_cell_text(t.rows[3].cells[0], "工程量签证意见", bold=True, name=HEI, size=9, align=CENTER)
    mcell(t, 3, 1, 2, "监理单位意见：\n\n监理单位（章）\n代表：\n日期：", size=9, align=LEFT)
    mcell(t, 3, 3, 5, "项目负责人意见：\n\n项目实施部门意见：\n项目实施部门（章）\n项目实施部门负责人：\n日期：", size=9, align=LEFT)
    set_col_widths(t, [2.5, 2.5, 2.5, 2.5, 2.5, 2.5])


# ---------------- 表单 5：日常维修包项目申请表 ----------------
def build_maintenance_apply(doc, items, ctx):
    add_title(doc, "日常维修包项目申请表（配网适用）")
    add_para(doc, "填表人：                                          填表时间：", size=10)
    doc.add_paragraph("")
    t = doc.add_table(rows=9, cols=6)
    set_table_borders(t)
    rows = [
        ("项目名称", ctx["project"], "项目编号", ctx["project_no"] or "________"),
        ("项目实施部门", ctx["bureau"] or "________", "项目负责人", ctx["manager"] or "________"),
        ("实施时间", ctx["period"] or "________", "施工单位", ctx["contractor"] or "________"),
    ]
    for i, (a, av, b, bv) in enumerate(rows):
        set_cell_text(t.rows[i].cells[0], a, bold=True, name=HEI, size=9.5)
        set_cell_text(t.rows[i].cells[1], av, size=9.5)
        set_cell_text(t.rows[i].cells[2], b, bold=True, name=HEI, size=9.5)
        set_cell_text(t.rows[i].cells[3], bv, size=9.5)
    set_cell_text(t.rows[3].cells[0], "项目总预算（万元）", bold=True, name=HEI, size=9.5)
    set_cell_text(t.rows[3].cells[1], "", size=9.5)
    set_cell_text(t.rows[3].cells[2], "累计完成预算（万元）", bold=True, name=HEI, size=9.5)
    set_cell_text(t.rows[3].cells[3], "", size=9.5)
    set_cell_text(t.rows[3].cells[4], "拟申请估算（万元）", bold=True, name=HEI, size=9.5)
    set_cell_text(t.rows[3].cells[5], "", size=9.5)
    content = "本次对 %s 范围内线路走廊树障进行修剪清理，共 %d 处，涉及电杆 %d 条，消除线路通道树障隐患。" % (
        ctx["station"] or ctx["bureau"] or "辖区", len(items),
        len({it.get("pole_no", "") for it in items}))
    mcell(t, 4, 0, 0, "实施原因、必要性", bold=True, name=HEI, size=9.5, align=LEFT)
    mcell(t, 4, 1, 5, "线路走廊树障危及配电线路安全，易引发跳闸、断线等事故，需及时修剪清理以消除隐患。", size=9.5, align=LEFT)
    mcell(t, 5, 0, 0, "主要实施内容", bold=True, name=HEI, size=9.5, align=LEFT)
    mcell(t, 5, 1, 5, content, size=9.5, align=LEFT)
    mcell(t, 6, 0, 0, "设备运维负责人意见", bold=True, name=HEI, size=9.5, align=LEFT)
    mcell(t, 6, 1, 5, "", size=9.5, align=LEFT)
    mcell(t, 7, 0, 0, "供电所配电业务员意见", bold=True, name=HEI, size=9.5, align=LEFT)
    mcell(t, 7, 1, 5, "", size=9.5, align=LEFT)
    mcell(t, 8, 0, 0, "供电所所领导意见", bold=True, name=HEI, size=9.5, align=LEFT)
    mcell(t, 8, 1, 5, "", size=9.5, align=LEFT)
    set_col_widths(t, [3.0, 3.0, 3.0, 3.0, 3.0, 3.0])
    doc.add_paragraph("")
    add_para(doc, "注：1、总费用不足5万元的日常维修项目由区局生计部审批。", size=9)
    add_para(doc, "      2、总费用5万元（含5万元）以上的日常维修项目由主管局领导审批。", size=9)


# ---------------- 表单 6：附录H 施工前后照片 ----------------
def _photo_block(doc, label, ftok, fetch):
    add_para(doc, label, name=HEI, size=10.5, bold=True)
    data = fetch(ftok) if (ftok and fetch) else None
    if data:
        try:
            doc.add_picture(io.BytesIO(data), width=Cm(8.0))
            doc.paragraphs[-1].alignment = CENTER
            return
        except Exception:
            pass
    # 占位框
    ph = doc.add_table(rows=1, cols=1)
    ph.rows[0].cells[0].width = Cm(8.0)
    set_cell_text(ph.rows[0].cells[0], "（无照片）", align=CENTER, size=9, name=CN)
    set_table_borders(ph)
    tr = ph.rows[0]._tr
    trPr = tr.get_or_add_trPr()
    h = OxmlElement('w:trHeight'); h.set(qn('w:val'), '3200'); trPr.append(h)


def build_appendix_photos(doc, items, ctx, fetch=None):
    add_title(doc, "附录H：施工前后照片")
    add_para(doc, "施工前、后照片", name=HEI, size=12, bold=True, align=CENTER)
    add_para(doc, "工程名称：" + ctx["project"])
    add_para(doc, "项目编号：" + (ctx["project_no"] or "________________"))
    doc.add_paragraph("")
    if not items:
        add_para(doc, "（本期无作业记录）", align=CENTER)
    for it in items:
        add_para(doc, "电杆：" + _loc(it), name=HEI, size=10.5, bold=True)
        before = (it.get("before") or [])
        after = (it.get("after") or [])
        bf = before[0].get("file_token") if before else None
        af = after[0].get("file_token") if after else None
        _photo_block(doc, "施工前图片：", bf, fetch)
        _photo_block(doc, "施工后图片：", af, fetch)
        doc.add_paragraph("")
    notes = ("备注：1、每个应急抢修工程需对应出具一组施工前、后照片(像素不低于800万);"
             "2、如一组照片数量不足以说明工程内容，可另附多页照片表达清楚;如黑白照片打印不清晰，应进行彩色打印;"
             "3、前、后对应照片尽量从相同角度拍摄，以便比对，且需同时附上全景及局部照片，"
             "必要时应进行圈划或补充文字说明。")
    add_para(doc, notes, size=9)


# ---------------- 打包 ----------------
def build_package(items, meta, out_dir, fetch=None):
    os.makedirs(out_dir, exist_ok=True)
    ctx = derive_ctx(items, meta)
    if fetch is None:
        fetch = _default_fetch
    specs = [
        ("线路走廊清理项目现场工程量签证单.docx", lambda d: build_visa_line_corridor(d, items, ctx)),
        ("工程量明细表.docx", lambda d: build_detail_table(d, items, ctx)),
        ("现场工程量签证表.docx", lambda d: build_site_visa(d, items, ctx)),
        ("工程量签证表.docx", lambda d: build_quantity_visa(d, items, ctx)),
        ("日常维修包项目申请表.docx", lambda d: build_maintenance_apply(d, items, ctx)),
        ("附录H施工前后照片.docx", lambda d: build_appendix_photos(d, items, ctx, fetch)),
    ]
    paths = []
    for fname, fn in specs:
        doc = Document()
        _setup_margins(doc)
        fn(doc)
        p = os.path.join(out_dir, fname)
        doc.save(p)
        paths.append(p)
    note = os.path.join(out_dir, "验收资料字段说明.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write(_build_notes(items, ctx))
    paths.append(note)
    return paths


def _build_notes(items, ctx):
    lines = []
    lines.append("验收资料字段说明（自动生成）")
    lines.append("生成时间：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("作业条数：" + str(len(items)))
    lines.append("项目名称：" + ctx["project"])
    lines.append("项目编号：" + (ctx["project_no"] or "（未提供）"))
    lines.append("施工单位：" + (ctx["contractor"] or "（未提供）"))
    lines.append("实施时间：" + (ctx["period"] or "（未提供）"))
    lines.append("")
    lines.append("字段映射：")
    lines.append("  实施地点/电杆编号 -> 作业记录.电杆编号（+位置描述）")
    lines.append("  树障种类 -> 作业记录.树木品种")
    lines.append("  施工作业计划编号 -> 作业记录.工作票编号")
    lines.append("  隐患等级/剪下树枝量/标示牌/备注 -> 作业记录对应字段（汇总进“说明/备注”列）")
    lines.append("")
    lines.append("需人工补充的字段（系统暂无对应数据）：")
    lines.append("  1. 规格/胸径：作业记录未采集树障胸径，已在“规格”列填“—”，请按实际胸径补填。")
    lines.append("  2. 工程量：每行按 1 处计（棵）；如需按实际棵数/车次统计，请按作业记录“剪下树枝量”手工修正。")
    lines.append("  3. 项目名称/项目编号/施工单位/项目经理/预算 等立项信息：需人工填写或经 --project/--contractor 传入。")
    lines.append("  4. 附录H 照片：取作业记录“修剪前/后照片”附件；无附件处为占位框，请补拍后替换。")
    return "\n".join(lines)


def pack_zip(paths, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, os.path.basename(p))
    return zip_path


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--package":
        in_path = sys.argv[2]
        out_arg = sys.argv[3] if len(sys.argv) > 3 else None
        with open(in_path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        meta = data.get("meta", {})
        if out_arg and out_arg.endswith(".zip"):
            out_dir = tempfile.mkdtemp(prefix="acc_pkg_")
            zip_path = out_arg
        else:
            out_dir = out_arg or tempfile.mkdtemp(prefix="acc_pkg_")
            zip_path = out_dir.rstrip("/") + ".zip"
        paths = build_package(items, meta, out_dir)
        pack_zip(paths, zip_path)
        print("saved:", zip_path)
        return
    print("usage: acceptance_docx.py --package <in.json> <out.zip>", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
