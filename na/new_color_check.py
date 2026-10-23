# -*- coding: utf-8 -*-
"""
新色跟进离线判定脚本 (验证版) —— 调用 new_color_module.compute_new_colors
v3 口径 (2026-09-21 用户确认, 在 v2 基础上):
- 套件链路: 上架SKU → SKU平台信息表(店铺+14K货号) → 子件(成员货品) = 已上架库存SKU
- 已上架明细按「上架SKU+库存SKU」一对一行; 未上架(新色)按「库存SKU」一行
- 店铺显示名: DECO / HN / Urban / XYT / JYT / Walmart

v5 口径 (2026-09-21 用户确认):
- SPU 主行 = 一个 SPU × 一个店铺 (同SPU多店上架 → 多条记录), 新色数/新色列表/待上架SKU数 均按该店铺口径
- 店铺运营规则: Walmart=综合组-WLM·Chloris; XYT/JYT=品牌组·MIA黄;
  Urban=该SPU款式表「综合组-AMZ」行的运营; DECO/HN=该SPU「非综合组-AMZ」行的运营; 查不到回退款式表原值
- 折叠展开只显示「该店铺 + 该SPU」的 SKU 明细
"""
import os
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
import new_color_module as ncm

F_OUT = BASE + r"\新色跟进_判定结果_20260921.xlsx"

if __name__ == '__main__':
    res = ncm.compute_new_colors(BASE)

    spu_rows = pd.DataFrame(res['spu_rows'], columns=['SPU', '店铺', '运营组', '运营', '已上架店铺', '新色数', '新色列表', '待上架SKU数', '款式表运营组', '款式表运营'])
    sku_rows = pd.DataFrame(res['sku_rows'], columns=['SPU', '上架SKU', '库存SKU', '颜色', '店铺', '上架状态', '颜色判定', '在库库存', '在途库存', '在途最早预计到仓', '库存状态', '数量'])
    new_colors = pd.DataFrame(res['new_colors'], columns=['SPU', '店铺', '新色', '颜色判定', '库存SKU', '在库库存', '在途库存', '在途最早预计到仓', '库存状态'])
    auto_df = pd.DataFrame(res.get('auto_colors') or [], columns=['原始码', '归一为'])
    auto_df['说明'] = '档案未直接命中, 按「缩写↔全称」归一为上述规范色名'
    pending = pd.DataFrame(res.get('pending_colors') or [],
                           columns=['颜色码', 'SPU数', 'SKU数', '新色行数', '示例SKU', '示例SPU', '涉及店铺'])
    warn_df = pd.DataFrame(res['warn_unmatched'], columns=['店铺', '未命中套件的非删除平台SKU', '状态'])
    warn_df['处理方式'] = '回退: 平台SKU自身按库存级SKU处理'
    warn2 = pd.DataFrame(res['warn_unparse'], columns=['店铺', 'SKU', 'SPU', '状态'])
    warn2['问题'] = '已上架SPU的在库/在途SKU无法解析颜色(不参与对比)'

    print('\n========== 判定结果 (v5, 页面口径) ==========')
    print(f'SPU×店铺 记录数: {len(spu_rows)}  (涉及 {spu_rows["SPU"].nunique() if len(spu_rows) else 0} 个 SPU)')
    if len(spu_rows):
        print('\n--- 各店铺 记录数 / 新色SKU数 ---')
        print(spu_rows.groupby('店铺').agg(记录数=('SPU', 'count'), SPU数=('SPU', 'nunique'),
                                          新色SKU数=('待上架SKU数', 'sum')).to_string())
        print('\n--- 店铺 × 运营组 分布 (规则应用结果) ---')
        print(spu_rows.groupby(['店铺', '运营组']).size().to_string())
    if len(sku_rows):
        print(f'\nSKU 明细行: {len(sku_rows)}  (已上架 {int((sku_rows["上架状态"]=="已上架").sum())} / 未上架 {int((sku_rows["上架状态"]=="未上架").sum())} / 非颜色 {int((sku_rows["上架状态"]=="非颜色").sum())})')
    print(f'待上架新色行: {len(new_colors)}  (颜色待确认 {int((new_colors["颜色判定"]=="待确认").sum()) if len(new_colors) else 0})')

    with pd.ExcelWriter(F_OUT, engine='openpyxl') as w:
        spu_rows.to_excel(w, sheet_name='SPU汇总(SPU×店铺)', index=False)
        sku_rows.to_excel(w, sheet_name='SKU明细', index=False)
        new_colors.to_excel(w, sheet_name='待上架新色', index=False)
        if len(auto_df):
            auto_df.to_excel(w, sheet_name='颜色缩写全称归一', index=False)
        if len(pending):
            pending.to_excel(w, sheet_name='待确认颜色', index=False)
        warn_df.to_excel(w, sheet_name='告警-未命中套件', index=False)
        warn2.to_excel(w, sheet_name='告警-颜色无法解析', index=False)

    print(f'\n已输出: {F_OUT}')
    print(f'告警: 未命中套件 {len(warn_df)} 条, 颜色无法解析 {len(warn2)} 条')
