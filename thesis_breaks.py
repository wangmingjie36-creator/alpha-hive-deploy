"""
🐝 Alpha Hive - Thesis Breaks 监控系统
优化 5：明确定义和监控失效条件
"""

import json
from datetime import datetime
from typing import List, Dict, Tuple

class ThesisBreakConfig:
    """针对不同标的的失效条件配置"""

    NVDA_BREAKS = {
        "level_1_warning": {
            "name": "预警级别",
            "score_reduction": -0.15,
            "conditions": [
                {
                    "id": "datacenter_revenue_decline",
                    "metric": "DataCenter Revenue Growth",
                    "trigger": "季度环比下降 > 5%",
                    "data_source": "季度财报",
                    "check_frequency": "季度",
                    "current_status": "✅ 正常（+8% QoQ）",
                    "severity": "HIGH"
                },
                {
                    "id": "competitor_threat",
                    "metric": "竞争对手新产品",
                    "trigger": "AMD 或 Intel 发布超越 NVIDIA 的产品",
                    "data_source": "产品发布公告",
                    "check_frequency": "实时",
                    "current_status": "✅ 无重大威胁",
                    "severity": "HIGH"
                },
                {
                    "id": "china_ban_risk",
                    "metric": "中国芯片禁令",
                    "trigger": "Polymarket 禁令概率 > 60%",
                    "data_source": "Polymarket",
                    "check_frequency": "实时",
                    "current_status": "⚠️ 中等风险（概率 35%）",
                    "severity": "CRITICAL"
                },
                {
                    "id": "margin_compression",
                    "metric": "毛利率",
                    "trigger": "季度环比下降 > 200bps",
                    "data_source": "财报",
                    "check_frequency": "季度",
                    "current_status": "✅ 稳定（72% 毛利率）",
                    "severity": "MEDIUM"
                }
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别",
            "recommendation_reverse": True,
            "conditions": [
                {
                    "id": "eps_miss_severe",
                    "metric": "EPS 大幅低于预期",
                    "trigger": "实际 < 预期 20%+",
                    "data_source": "财报披露",
                    "check_frequency": "季度",
                    "current_status": "✅ 未发生",
                    "action": "立即转向空头或对冲"
                },
                {
                    "id": "export_ban",
                    "metric": "美国芯片出口禁令",
                    "trigger": "直接禁止对华 H100/H800 销售",
                    "data_source": "商务部公告",
                    "check_frequency": "实时",
                    "current_status": "⚠️ 监管风险中等",
                    "action": "财务影响：假设 4% 总收入"
                },
                {
                    "id": "ceo_departure",
                    "metric": "CEO 离职",
                    "trigger": "Jensen Huang 宣布离职",
                    "data_source": "公司公告",
                    "check_frequency": "实时",
                    "current_status": "✅ 无异常",
                    "action": "战略不确定性极高"
                }
            ]
        }
    }

    VKTX_BREAKS = {
        "level_1_warning": {
            "name": "预警级别",
            "score_reduction": -0.15,
            "conditions": [
                {
                    "id": "trial_dropout",
                    "metric": "临床试验患者脱落",
                    "trigger": "关键患者脱落 > 15%",
                    "data_source": "试验数据",
                    "check_frequency": "持续",
                    "current_status": "✅ 未报告异常",
                    "severity": "CRITICAL"
                },
                {
                    "id": "competitor_data",
                    "metric": "竞争对手试验数据",
                    "trigger": "发布更优越的数据",
                    "data_source": "学术会议/FDA 公告",
                    "check_frequency": "实时",
                    "current_status": "✅ 无重大威胁",
                    "severity": "HIGH"
                }
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别",
            "recommendation_reverse": True,
            "conditions": [
                {
                    "id": "fda_hold",
                    "metric": "FDA 临床试验暂停",
                    "trigger": "IND Hold（试验中止令）",
                    "data_source": "FDA 官方",
                    "check_frequency": "实时",
                    "current_status": "✅ 未发生",
                    "action": "股价可能暴跌 30-50%"
                },
                {
                    "id": "trial_failure",
                    "metric": "Phase 3 试验失败",
                    "trigger": "关键终点未达到统计学意义",
                    "data_source": "试验结果发布",
                    "check_frequency": "按计划发布日期",
                    "current_status": "⏳ 预期 2026-Q3 发布",
                    "action": "股价可能下跌 60-80%"
                }
            ]
        }
    }

    TSLA_BREAKS = {
        "level_1_warning": {
            "name": "预警级别",
            "score_reduction": -0.15,
            "conditions": [
                {
                    "id": "delivery_decline",
                    "metric": "季度交付量",
                    "trigger": "同比下降 > 5%",
                    "data_source": "Tesla 官方数据",
                    "check_frequency": "季度",
                    "current_status": "✅ YTD +8% 交付量",
                    "severity": "HIGH"
                },
                {
                    "id": "margin_drop",
                    "metric": "Gross Margin",
                    "trigger": "环比下降 > 200bps",
                    "data_source": "财报",
                    "check_frequency": "季度",
                    "current_status": "⚠️ 18.0% (历史平均 20%)",
                    "severity": "MEDIUM"
                }
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别",
            "recommendation_reverse": True,
            "conditions": [
                {
                    "id": "elon_departure",
                    "metric": "Elon Musk 离职",
                    "trigger": "CEO 卸任或重大丑闻",
                    "data_source": "公司公告",
                    "check_frequency": "实时",
                    "current_status": "✅ 无异常",
                    "action": "股价可能下跌 10-20%"
                },
                {
                    "id": "revenue_miss",
                    "metric": "财报收入",
                    "trigger": "实际 < 预期 15%+",
                    "data_source": "财报",
                    "check_frequency": "季度",
                    "current_status": "✅ 未发生",
                    "action": "完全停止看多推荐"
                }
            ]
        }
    }

    # ── Technology ──────────────────────────────────────────────────────────

    MSFT_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "azure_growth_slowdown", "metric": "Azure 收入增速",
                 "trigger": "季度 YoY 增速低于 20%", "data_source": "季度财报",
                 "check_frequency": "季度", "current_status": "✅ 正常（+29% YoY）", "severity": "HIGH"},
                {"id": "copilot_adoption", "metric": "Copilot 商业化进展",
                 "trigger": "付费席位季度净增 < 5%", "data_source": "财报/分析师会议",
                 "check_frequency": "季度", "current_status": "✅ 持续扩张", "severity": "MEDIUM"},
                {"id": "antitrust_risk", "metric": "反垄断监管",
                 "trigger": "EU/DOJ 提起重大诉讼或强制拆分令", "data_source": "监管公告",
                 "check_frequency": "实时", "current_status": "✅ 无重大诉讼", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "cloud_market_share_loss", "metric": "云市场份额",
                 "trigger": "Azure 份额季度环比下降 > 2pp", "data_source": "Synergy Research",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "重估云业务增长预期"},
                {"id": "msft_eps_miss", "metric": "EPS 大幅低于预期",
                 "trigger": "实际 < 预期 15%+", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "重新评估估值模型"},
            ]
        }
    }

    AMD_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "mi300_adoption", "metric": "MI300X GPU 份额",
                 "trigger": "大客户（Meta/AWS）明确转回 NVDA", "data_source": "公司公告/分析师报告",
                 "check_frequency": "实时", "current_status": "✅ 份额持续增加", "severity": "HIGH"},
                {"id": "pc_cpu_inventory", "metric": "PC CPU 库存",
                 "trigger": "渠道库存去化周期 > 12 周", "data_source": "财报/供应链追踪",
                 "check_frequency": "季度", "current_status": "✅ 库存正常", "severity": "MEDIUM"},
                {"id": "amd_china_export", "metric": "中国出口限制",
                 "trigger": "MI300 系列被纳入出口管制清单", "data_source": "BIS 公告",
                 "check_frequency": "实时", "current_status": "⚠️ 持续监控", "severity": "CRITICAL"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "amd_eps_miss", "metric": "数据中心 GPU 营收",
                 "trigger": "季度数据中心营收低于预期 20%+", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "重估 AI GPU 市场份额假设"},
                {"id": "lisa_su_departure", "metric": "CEO 离职",
                 "trigger": "Lisa Su 宣布离职", "data_source": "公司公告",
                 "check_frequency": "实时", "current_status": "✅ 无异常",
                 "action": "战略不确定性大幅上升"},
            ]
        }
    }

    QCOM_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "apple_modem_inhouse", "metric": "Apple 自研基带",
                 "trigger": "Apple 正式宣布将自研基带用于旗舰 iPhone", "data_source": "Apple 发布会/分析师报告",
                 "check_frequency": "实时", "current_status": "⚠️ Apple 自研进展中", "severity": "CRITICAL"},
                {"id": "qcom_handset_shipment", "metric": "手机芯片出货量",
                 "trigger": "全球智能手机出货量 YoY 下滑 > 8%", "data_source": "IDC/Counterpoint",
                 "check_frequency": "季度", "current_status": "✅ 市场温和复苏", "severity": "HIGH"},
                {"id": "arm_license_dispute", "metric": "ARM 授权纠纷",
                 "trigger": "ARM 终止或限制 QCOM 授权", "data_source": "法庭文件/公司公告",
                 "check_frequency": "实时", "current_status": "⚠️ 法律诉讼持续", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "apple_transition_complete", "metric": "Apple 完成基带替换",
                 "trigger": "iPhone 正式搭载 Apple 自研基带", "data_source": "产品发布",
                 "check_frequency": "实时", "current_status": "✅ 预计 2026 后",
                 "action": "手机业务收入大幅缩减，重估估值"},
                {"id": "qcom_china_ban", "metric": "中国业务受限",
                 "trigger": "中国监管禁止采购 QCOM 芯片", "data_source": "监管公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "约 25% 收入风险敞口"},
            ]
        }
    }

    META_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "dau_growth", "metric": "日活跃用户增速",
                 "trigger": "全球 DAU 季度 QoQ 增速 < 1%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 持续增长", "severity": "HIGH"},
                {"id": "ad_revenue_softness", "metric": "广告收入增速",
                 "trigger": "广告收入 YoY 增速 < 10%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 增速强劲", "severity": "HIGH"},
                {"id": "regulation_risk", "metric": "监管/隐私风险",
                 "trigger": "EU/FTC 裁定重大罚款（> $5B）或强制拆分", "data_source": "监管公告",
                 "check_frequency": "实时", "current_status": "⚠️ EU 持续审查", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "meta_reality_labs_loss", "metric": "Reality Labs 亏损",
                 "trigger": "单季亏损超 $6B 且无改善路径", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "⚠️ 持续亏损中",
                 "action": "重新评估元宇宙战略价值"},
                {"id": "tiktok_ban_reversal", "metric": "TikTok 解禁",
                 "trigger": "TikTok 在美恢复全面运营", "data_source": "法规/法庭决定",
                 "check_frequency": "实时", "current_status": "⚠️ 监控中",
                 "action": "广告份额回流压力，降低收入预期"},
            ]
        }
    }

    BILI_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "bili_mau_decline", "metric": "月活跃用户",
                 "trigger": "季度 MAU 同比下滑 > 3%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 稳定增长", "severity": "HIGH"},
                {"id": "bili_monetization", "metric": "商业化能力",
                 "trigger": "ARPU 季度同比下降 > 10%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 持续改善", "severity": "MEDIUM"},
                {"id": "china_content_regulation", "metric": "内容监管",
                 "trigger": "监管要求下架核心内容分类或限制直播", "data_source": "监管公告",
                 "check_frequency": "实时", "current_status": "✅ 合规运营", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "bili_delisting_risk", "metric": "退市风险",
                 "trigger": "SEC 或纳斯达克发出退市警告", "data_source": "SEC 公告",
                 "check_frequency": "实时", "current_status": "✅ 正常上市",
                 "action": "立即关注 ADR 与港股价差"},
                {"id": "bili_cash_burn", "metric": "现金消耗",
                 "trigger": "自由现金流连续 3 季度为负且无明确改善路径", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 现金流改善中",
                 "action": "评估融资风险和稀释可能性"},
            ]
        }
    }

    AMZN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "aws_growth_decel", "metric": "AWS 收入增速",
                 "trigger": "季度 YoY 增速低于 15%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 增速回升", "severity": "HIGH"},
                {"id": "retail_margin", "metric": "北美零售利润率",
                 "trigger": "营业利润率 QoQ 下降 > 150bps", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 利润率改善", "severity": "MEDIUM"},
                {"id": "antitrust_ftc", "metric": "FTC 反垄断",
                 "trigger": "法院要求拆分零售或云业务", "data_source": "法庭文件",
                 "check_frequency": "实时", "current_status": "⚠️ FTC 诉讼进行中", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "aws_customer_churn", "metric": "AWS 主要客户流失",
                 "trigger": "前 10 大客户中超 3 家迁移至 Azure/GCP", "data_source": "分析师报告",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "重新评估云市场格局和 AMZN 估值"},
                {"id": "amzn_eps_miss", "metric": "综合 EPS 大幅低于预期",
                 "trigger": "实际 EPS < 预期 20%+", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "降低盈利预期，重估 PE 倍数"},
            ]
        }
    }

    # ── Healthcare / Biotech ─────────────────────────────────────────────────

    AMGN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "enbrel_biosimilar", "metric": "Enbrel 仿制药竞争",
                 "trigger": "仿制药市场份额超过 20%", "data_source": "IMS Health/IQVIA",
                 "check_frequency": "季度", "current_status": "⚠️ 仿制药压力持续", "severity": "HIGH"},
                {"id": "obesity_drug_pipeline", "metric": "肥胖症管线进展",
                 "trigger": "MariTide Phase 3 数据低于预期", "data_source": "临床试验公告",
                 "check_frequency": "按里程碑", "current_status": "✅ Phase 3 进行中", "severity": "CRITICAL"},
                {"id": "amgn_pricing_risk", "metric": "药品定价压力",
                 "trigger": "IRA 谈判药品扩大至 AMGN 核心品种", "data_source": "CMS 公告",
                 "check_frequency": "实时", "current_status": "⚠️ 监控中", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "amgn_trial_failure", "metric": "核心管线失败",
                 "trigger": "MariTide 或重磅在研品种 Phase 3 失败", "data_source": "试验结果",
                 "check_frequency": "按里程碑", "current_status": "✅ 未发生",
                 "action": "重估增长驱动力，股价可能下跌 15-25%"},
                {"id": "amgn_revenue_miss", "metric": "收入大幅低于预期",
                 "trigger": "季度收入 < 预期 10%+", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "降低全年指引预期"},
            ]
        }
    }

    BIIB_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "leqembi_adoption", "metric": "Leqembi 销售额",
                 "trigger": "季度销售额 < $200M（不及预期 50%）", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "⚠️ 爬坡缓慢", "severity": "CRITICAL"},
                {"id": "alzheimer_safety", "metric": "Leqembi 安全性问题",
                 "trigger": "FDA 发出黑框警告或限制使用", "data_source": "FDA 公告",
                 "check_frequency": "实时", "current_status": "✅ 正常监测", "severity": "HIGH"},
                {"id": "biib_pipeline", "metric": "管线进展",
                 "trigger": "2 个以上 Phase 3 项目同期失败", "data_source": "临床试验公告",
                 "check_frequency": "按里程碑", "current_status": "✅ 管线多元化", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "biib_leqembi_recall", "metric": "Leqembi 市场撤回",
                 "trigger": "因严重副作用被 FDA 要求撤市", "data_source": "FDA 公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "股价可能暴跌 40-60%"},
                {"id": "biib_revenue_cliff", "metric": "专利悬崖",
                 "trigger": "核心收入产品专利到期且仿制药上市", "data_source": "FDA 橙皮书",
                 "check_frequency": "年度", "current_status": "⚠️ 2027 起部分到期",
                 "action": "重新评估收入持续性"},
            ]
        }
    }

    JNJ_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "jnj_talc_liability", "metric": "石棉诉讼赔偿",
                 "trigger": "法院判决总赔偿金额超过 $10B", "data_source": "法庭文件",
                 "check_frequency": "实时", "current_status": "⚠️ 诉讼持续", "severity": "HIGH"},
                {"id": "jnj_pharma_growth", "metric": "制药业务增速",
                 "trigger": "制药板块收入 YoY 增速 < 3%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 由 Darzalex 驱动增长", "severity": "MEDIUM"},
                {"id": "darzalex_competition", "metric": "Darzalex 竞争",
                 "trigger": "新型 BCMA 类药物明显蚕食份额", "data_source": "医药销售数据",
                 "check_frequency": "季度", "current_status": "✅ 份额稳固", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "jnj_dividend_cut", "metric": "股息削减",
                 "trigger": "JNJ 宣布削减季度股息", "data_source": "公司公告",
                 "check_frequency": "季度", "current_status": "✅ 连续增长 60+ 年",
                 "action": "价值投资假设破坏，机构减持风险"},
                {"id": "jnj_major_recall", "metric": "重大产品召回",
                 "trigger": "FDA 强制召回 JNJ 核心药品或医疗器械", "data_source": "FDA 公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "视规模可能影响 5-15% 营收"},
            ]
        }
    }

    REGN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "eylea_biosimilar", "metric": "Eylea 仿制药压力",
                 "trigger": "FDA 批准 Eylea 仿制药且处方份额 > 15%", "data_source": "IQVIA",
                 "check_frequency": "季度", "current_status": "⚠️ 仿制药进入市场", "severity": "CRITICAL"},
                {"id": "dupixent_growth", "metric": "Dupixent 增速",
                 "trigger": "季度销售额 YoY 增速低于 15%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 强劲增长", "severity": "HIGH"},
                {"id": "regn_oncology_pipeline", "metric": "肿瘤管线",
                 "trigger": "主要肿瘤药 Phase 3 初读数据低于预期", "data_source": "试验公告",
                 "check_frequency": "按里程碑", "current_status": "✅ Odronextamab 推进中", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "regn_ira_negotiation", "metric": "IRA 药价谈判",
                 "trigger": "Dupixent 被选入 IRA 谈判且降价 > 25%", "data_source": "CMS",
                 "check_frequency": "实时", "current_status": "⚠️ 2026 起风险",
                 "action": "重估 Dupixent 长期收入模型"},
                {"id": "regn_pipeline_failure", "metric": "多管线同期失败",
                 "trigger": "2 个以上 Phase 3 主要项目同时失败", "data_source": "临床试验",
                 "check_frequency": "按里程碑", "current_status": "✅ 未发生",
                 "action": "估值大幅重置"},
            ]
        }
    }

    # ── Clean Energy ─────────────────────────────────────────────────────────

    PLUG_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "plug_cash_runway", "metric": "现金跑道",
                 "trigger": "剩余现金 < 6 个月运营费用", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "⚠️ 融资压力持续", "severity": "CRITICAL"},
                {"id": "hydrogen_delivery_miss", "metric": "氢气交付能力",
                 "trigger": "季度氢气交付量低于承诺 30%+", "data_source": "公司运营披露",
                 "check_frequency": "季度", "current_status": "⚠️ 持续低于预期", "severity": "HIGH"},
                {"id": "doa_subsidy_cuts", "metric": "政府补贴政策",
                 "trigger": "IRA 清洁能源税收抵免被大幅削减", "data_source": "国会立法",
                 "check_frequency": "实时", "current_status": "✅ 补贴政策稳定", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "plug_going_concern", "metric": "持续经营警告",
                 "trigger": "审计师出具'持续经营'疑虑意见", "data_source": "年报",
                 "check_frequency": "年度", "current_status": "⚠️ 历史上曾出现",
                 "action": "退出所有多头仓位"},
                {"id": "plug_nasdaq_warning", "metric": "纳斯达克合规警告",
                 "trigger": "收到纳斯达克退市警告函", "data_source": "SEC 8-K",
                 "check_frequency": "实时", "current_status": "✅ 目前合规",
                 "action": "立即评估退市风险"},
            ]
        }
    }

    RUN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "solar_installation_slowdown", "metric": "装机量",
                 "trigger": "季度新增装机量 QoQ 下滑 > 15%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 安装量增长", "severity": "HIGH"},
                {"id": "interest_rate_sensitivity", "metric": "利率敏感性",
                 "trigger": "10 年期国债收益率 > 5.5%", "data_source": "Fed/Bloomberg",
                 "check_frequency": "实时", "current_status": "✅ 当前利率可接受", "severity": "HIGH"},
                {"id": "net_metering_policy", "metric": "净电表政策",
                 "trigger": "主要州大幅削减上网电价补贴", "data_source": "州级监管公告",
                 "check_frequency": "实时", "current_status": "⚠️ CA/NEM3 已影响", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "run_refinancing_risk", "metric": "ABS 再融资风险",
                 "trigger": "无法完成季度 ABS 发行或利差超 500bps", "data_source": "债券市场",
                 "check_frequency": "季度", "current_status": "✅ 融资渠道通畅",
                 "action": "流动性压力极大，重估商业模式"},
                {"id": "ira_credit_reversal", "metric": "ITC 税收抵免削减",
                 "trigger": "太阳能 ITC 从 30% 降至 15% 以下", "data_source": "国会立法",
                 "check_frequency": "实时", "current_status": "✅ 30% ITC 有效",
                 "action": "单位经济模型根本改变"},
            ]
        }
    }

    NEE_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "nee_rate_risk", "metric": "利率风险",
                 "trigger": "10 年期国债收益率 > 5.0%（公用事业估值压制）", "data_source": "Fed/Bloomberg",
                 "check_frequency": "实时", "current_status": "✅ 利率走向下行", "severity": "HIGH"},
                {"id": "renewable_permitting", "metric": "可再生能源审批",
                 "trigger": "联邦层面大型项目审批暂停超 6 个月", "data_source": "FERC 公告",
                 "check_frequency": "实时", "current_status": "✅ 正常推进", "severity": "MEDIUM"},
                {"id": "nee_dividend_growth", "metric": "股息增长承诺",
                 "trigger": "宣布低于承诺 10% 年股息增速", "data_source": "投资者日/公司公告",
                 "check_frequency": "年度", "current_status": "✅ 承诺 10% 年增", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "nee_capex_overrun", "metric": "资本支出超支",
                 "trigger": "年度 CapEx 超预算 > 20%", "data_source": "财报",
                 "check_frequency": "年度", "current_status": "✅ 资本纪律良好",
                 "action": "FCF 压力，可能影响股息安全性"},
                {"id": "nee_credit_downgrade", "metric": "信用评级下调",
                 "trigger": "穆迪/S&P 将 NEE 降至 BBB- 以下", "data_source": "评级机构",
                 "check_frequency": "实时", "current_status": "✅ A- 评级",
                 "action": "融资成本上升，债务密集型商业模式受损"},
            ]
        }
    }

    ICLN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "ira_rollback", "metric": "IRA 政策逆转",
                 "trigger": "国会通过削减 IRA 清洁能源条款的法案", "data_source": "国会记录",
                 "check_frequency": "实时", "current_status": "⚠️ 政策风险中等", "severity": "CRITICAL"},
                {"id": "clean_energy_rate_sensitivity", "metric": "利率敏感性",
                 "trigger": "10 年期收益率连续 3 个月 > 5%", "data_source": "Fed/Bloomberg",
                 "check_frequency": "实时", "current_status": "✅ 利率下行趋势", "severity": "HIGH"},
                {"id": "etf_outflows", "metric": "ETF 资金流出",
                 "trigger": "连续 4 周净流出超 $500M", "data_source": "ETF.com/Bloomberg",
                 "check_frequency": "每周", "current_status": "✅ 资金流入稳定", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "icln_major_holding_collapse", "metric": "重仓股大跌",
                 "trigger": "前 3 大持仓任一单季下跌 > 40%", "data_source": "市场数据",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "ETF 净值传导性下跌，评估行业系统性风险"},
                {"id": "clean_energy_subsidy_zero", "metric": "补贴归零",
                 "trigger": "联邦政府完全废除可再生能源补贴", "data_source": "国会立法",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "行业基本面根本改变"},
            ]
        }
    }

    ENPH_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "enph_eu_slowdown", "metric": "欧洲业务",
                 "trigger": "欧洲季度收入 QoQ 下滑 > 20%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "⚠️ 欧洲需求疲软", "severity": "HIGH"},
                {"id": "enph_inventory_correction", "metric": "渠道库存",
                 "trigger": "分销商库存去化周期超 16 周", "data_source": "财报/分销商数据",
                 "check_frequency": "季度", "current_status": "✅ 库存消化完毕", "severity": "HIGH"},
                {"id": "iq8_competition", "metric": "微型逆变器竞争",
                 "trigger": "SolarEdge 或新进入者以低 20%+ 价格抢占市场", "data_source": "行业数据",
                 "check_frequency": "季度", "current_status": "✅ 技术壁垒保持", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "enph_guidance_cut", "metric": "指引大幅下调",
                 "trigger": "下季度收入指引中点低于市场预期 20%+", "data_source": "财报电话会",
                 "check_frequency": "季度", "current_status": "✅ 未发生",
                 "action": "重估复苏节奏，警惕连续下调"},
                {"id": "enph_tariff_impact", "metric": "关税影响",
                 "trigger": "新增关税导致产品成本上升 > 15%", "data_source": "贸易政策公告",
                 "check_frequency": "实时", "current_status": "⚠️ 关税风险持续",
                 "action": "毛利率压缩，降低盈利预期"},
            ]
        }
    }

    # ── FinTech ───────────────────────────────────────────────────────────────

    SQ_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "sq_gpv_growth", "metric": "GPV 增速",
                 "trigger": "季度 GPV YoY 增速 < 8%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 双位数增长", "severity": "HIGH"},
                {"id": "cash_app_mau", "metric": "Cash App 月活",
                 "trigger": "MAU 季度环比下滑 > 2%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 稳健增长", "severity": "HIGH"},
                {"id": "sq_credit_losses", "metric": "贷款信用损失",
                 "trigger": "BNPL/贷款净坏账率 > 4%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 坏账率可控", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "sq_regulatory_fintech", "metric": "金融科技监管",
                 "trigger": "CFPB 或 OCC 强制要求 Square 申请银行牌照", "data_source": "监管公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "合规成本大幅上升，商业模式受限"},
                {"id": "jack_dorsey_departure", "metric": "创始人离职",
                 "trigger": "Jack Dorsey 宣布卸任 CEO", "data_source": "公司公告",
                 "check_frequency": "实时", "current_status": "✅ 在任",
                 "action": "战略方向不确定性上升"},
            ]
        }
    }

    COIN_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "crypto_trading_volume", "metric": "加密货币交易量",
                 "trigger": "行业季度交易量 QoQ 下滑 > 30%", "data_source": "链上数据/CoinGecko",
                 "check_frequency": "月度", "current_status": "✅ 牛市交易量充裕", "severity": "HIGH"},
                {"id": "sec_regulatory_action", "metric": "SEC 监管行动",
                 "trigger": "SEC 对 COIN 提起新的重大诉讼或执法行动", "data_source": "SEC 公告",
                 "check_frequency": "实时", "current_status": "⚠️ 监管环境改善中", "severity": "CRITICAL"},
                {"id": "coin_market_share", "metric": "交易所市场份额",
                 "trigger": "美国现货市场份额 < 40%", "data_source": "CoinMarketCap",
                 "check_frequency": "月度", "current_status": "✅ 份额主导", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "crypto_bear_market", "metric": "加密熊市",
                 "trigger": "BTC 价格从高点下跌 > 50% 且持续 3 个月", "data_source": "CoinGecko",
                 "check_frequency": "实时", "current_status": "✅ 牛市环境",
                 "action": "交易量和收入可能腰斩"},
                {"id": "coin_hack_or_breach", "metric": "安全漏洞",
                 "trigger": "Coinbase 交易所遭受重大黑客攻击", "data_source": "公司公告/链上监控",
                 "check_frequency": "实时", "current_status": "✅ 无已知漏洞",
                 "action": "用户信任崩溃，立即评估损失规模"},
            ]
        }
    }

    MSTR_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "btc_price_decline", "metric": "BTC 价格",
                 "trigger": "BTC 从当前高点下跌 > 35%", "data_source": "CoinGecko/Bloomberg",
                 "check_frequency": "实时", "current_status": "✅ BTC 价格强劲", "severity": "CRITICAL"},
                {"id": "mstr_premium_to_nav", "metric": "溢价率",
                 "trigger": "MSTR 股价溢价于 BTC NAV 超过 100%", "data_source": "市场数据",
                 "check_frequency": "实时", "current_status": "⚠️ 高溢价风险", "severity": "HIGH"},
                {"id": "mstr_debt_covenants", "metric": "债务契约",
                 "trigger": "因 BTC 价值下跌触发债务契约条款", "data_source": "公司债券文件",
                 "check_frequency": "实时", "current_status": "✅ 当前安全边际充足", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "btc_crash_severe", "metric": "BTC 暴跌",
                 "trigger": "BTC 从高点下跌 > 60%", "data_source": "CoinGecko",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "强制平仓风险，MSTR 股价可能跌幅超 BTC 本身"},
                {"id": "mstr_forced_selling", "metric": "强制卖出 BTC",
                 "trigger": "公司公告因债务压力被迫出售 BTC", "data_source": "公司公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "市场信心崩溃，立即清仓"},
            ]
        }
    }

    CRCL_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "usdc_market_share", "metric": "USDC 市场份额",
                 "trigger": "USDC 全球稳定币市场份额 < 20%", "data_source": "CoinMarketCap/DeFiLlama",
                 "check_frequency": "月度", "current_status": "✅ 份额稳定", "severity": "HIGH"},
                {"id": "stablecoin_regulation", "metric": "稳定币监管",
                 "trigger": "美国国会通过限制商业稳定币运营的法案", "data_source": "国会立法",
                 "check_frequency": "实时", "current_status": "⚠️ 监管立法进行中", "severity": "CRITICAL"},
                {"id": "crcl_revenue_concentration", "metric": "收入集中度",
                 "trigger": "利率收入占总收入比例 > 80% 且利率开始下行", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "⚠️ 利率敏感", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "crcl_depeg_event", "metric": "USDC 脱钩事件",
                 "trigger": "USDC 价格跌破 $0.97 超过 24 小时", "data_source": "CoinGecko",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "信任危机，CRCL 商业模式受根本性威胁"},
                {"id": "crcl_competitor_cbdc", "metric": "央行数字货币竞争",
                 "trigger": "美联储正式发行 CBDC（数字美元）", "data_source": "Fed 公告",
                 "check_frequency": "实时", "current_status": "✅ 尚无时间表",
                 "action": "稳定币私营商业模式受重大竞争冲击"},
            ]
        }
    }

    # ── AI ───────────────────────────────────────────────────────────────────

    UPST_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "upst_bank_partners", "metric": "贷款合作银行数量",
                 "trigger": "季度活跃银行/信用合作社数量 QoQ 下降 > 10%", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 合作方持续增加", "severity": "CRITICAL"},
                {"id": "upst_default_rate", "metric": "贷款违约率",
                 "trigger": "AI 模型预测违约率 vs 实际违约率偏差 > 15%", "data_source": "财报/投资者披露",
                 "check_frequency": "季度", "current_status": "⚠️ 历史上曾出现偏差", "severity": "HIGH"},
                {"id": "macro_credit_tightening", "metric": "信贷环境",
                 "trigger": "美国个人消费信贷违约率 YoY 上升 > 100bps", "data_source": "纽联储/Fed 数据",
                 "check_frequency": "月度", "current_status": "✅ 信贷质量改善", "severity": "HIGH"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "upst_funding_collapse", "metric": "贷款资金来源",
                 "trigger": "主要银行合作方（>20% 贷款量来源）终止合作", "data_source": "公司公告",
                 "check_frequency": "实时", "current_status": "✅ 未发生",
                 "action": "贷款发放量可能大幅萎缩"},
                {"id": "upst_model_failure", "metric": "AI 模型失效",
                 "trigger": "连续 2 季度实际违约率超模型预测 30%+", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 模型表现合理",
                 "action": "核心竞争力受损，重估 AI 信贷模式价值"},
            ]
        }
    }

    # ── Aerospace ────────────────────────────────────────────────────────────

    RKLB_BREAKS = {
        "level_1_warning": {
            "name": "预警级别", "score_reduction": -0.15,
            "conditions": [
                {"id": "launch_failure", "metric": "发射失败",
                 "trigger": "Electron 连续 2 次发射失败", "data_source": "公司公告/FAA",
                 "check_frequency": "实时", "current_status": "✅ 发射成功率 ~90%", "severity": "CRITICAL"},
                {"id": "neutron_delay", "metric": "Neutron 火箭进度",
                 "trigger": "Neutron 首飞推迟超 12 个月", "data_source": "公司更新",
                 "check_frequency": "季度", "current_status": "⚠️ 时间表延迟风险", "severity": "HIGH"},
                {"id": "rklb_backlog", "metric": "发射订单积压",
                 "trigger": "季度新签合同价值低于 $50M", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 订单积压充足", "severity": "MEDIUM"},
            ]
        },
        "level_2_stop_loss": {
            "name": "认输级别", "recommendation_reverse": True,
            "conditions": [
                {"id": "spacex_smallsat_compete", "metric": "SpaceX 竞争",
                 "trigger": "SpaceX 开始提供类似 Electron 的小卫星专属发射服务并低价 20%+", "data_source": "SpaceX 公告",
                 "check_frequency": "实时", "current_status": "✅ 市场定位差异化",
                 "action": "小型火箭市场护城河削弱"},
                {"id": "rklb_cash_depletion", "metric": "现金耗尽",
                 "trigger": "现金储备 < 6 个月运营，且融资无望", "data_source": "财报",
                 "check_frequency": "季度", "current_status": "✅ 现金储备充足",
                 "action": "增发或破产风险"},
            ]
        }
    }

    @classmethod
    def get_breaks_config(cls, ticker: str) -> Dict:
        """获取特定标的的失效条件"""
        configs = {
            "NVDA": cls.NVDA_BREAKS,
            "VKTX": cls.VKTX_BREAKS,
            "TSLA": cls.TSLA_BREAKS,
            "MSFT": cls.MSFT_BREAKS,
            "AMD":  cls.AMD_BREAKS,
            "QCOM": cls.QCOM_BREAKS,
            "META": cls.META_BREAKS,
            "BILI": cls.BILI_BREAKS,
            "AMZN": cls.AMZN_BREAKS,
            "AMGN": cls.AMGN_BREAKS,
            "BIIB": cls.BIIB_BREAKS,
            "JNJ":  cls.JNJ_BREAKS,
            "REGN": cls.REGN_BREAKS,
            "PLUG": cls.PLUG_BREAKS,
            "RUN":  cls.RUN_BREAKS,
            "NEE":  cls.NEE_BREAKS,
            "ICLN": cls.ICLN_BREAKS,
            "ENPH": cls.ENPH_BREAKS,
            "SQ":   cls.SQ_BREAKS,
            "COIN": cls.COIN_BREAKS,
            "MSTR": cls.MSTR_BREAKS,
            "CRCL": cls.CRCL_BREAKS,
            "UPST": cls.UPST_BREAKS,
            "RKLB": cls.RKLB_BREAKS,
        }
        return configs.get(ticker, {})

    @classmethod
    def get_coverage_info(cls) -> Dict:
        """返回失效条件覆盖统计"""
        all_tickers = [
            "NVDA","TSLA","MSFT","AMD","QCOM","META","BILI","AMZN",
            "VKTX","AMGN","BIIB","JNJ","REGN",
            "PLUG","RUN","NEE","ICLN","ENPH",
            "SQ","COIN","MSTR","CRCL",
            "UPST","RKLB",
        ]
        covered = [t for t in all_tickers if cls.get_breaks_config(t)]
        return {
            "total": len(all_tickers),
            "covered": len(covered),
            "coverage_pct": round(len(covered) / len(all_tickers) * 100),
            "covered_tickers": covered,
            "missing_tickers": [t for t in all_tickers if t not in covered],
        }


class ThesisBreakMonitor:
    """实时监控失效条件"""

    def __init__(self, ticker: str, initial_score: float):
        self.ticker = ticker
        self.initial_score = initial_score
        self.config = ThesisBreakConfig.get_breaks_config(ticker)
        self.alerts = []
        self.adjusted_score = initial_score

    def check_all_conditions(self, metric_data: Dict) -> Dict:
        """检查所有失效条件"""

        result = {
            "ticker": self.ticker,
            "timestamp": datetime.now().isoformat(),
            "level_1_warnings": [],
            "level_2_stops": [],
            "score_adjustment": 0,
            "final_score": self.initial_score
        }

        # 检查 Level 1 预警
        if "level_1_warning" in self.config:
            for condition in self.config["level_1_warning"]["conditions"]:
                if self._check_condition(condition, metric_data):
                    result["level_1_warnings"].append({
                        "condition_id": condition["id"],
                        "metric": condition["metric"],
                        "trigger": condition["trigger"],
                        "current_value": metric_data.get(condition["id"]),
                        "severity": condition.get("severity", "MEDIUM"),
                        "timestamp": datetime.now().isoformat()
                    })
                    result["score_adjustment"] -= 0.15

        # 检查 Level 2 认输
        if "level_2_stop_loss" in self.config:
            for condition in self.config["level_2_stop_loss"]["conditions"]:
                if self._check_condition(condition, metric_data):
                    result["level_2_stops"].append({
                        "condition_id": condition["id"],
                        "metric": condition["metric"],
                        "trigger": condition["trigger"],
                        "current_value": metric_data.get(condition["id"]),
                        "action": condition.get("action"),
                        "timestamp": datetime.now().isoformat()
                    })
                    result["score_adjustment"] -= 0.30  # Level 2 更严重

        # 计算最终评分
        result["final_score"] = max(0, min(10, self.initial_score + result["score_adjustment"]))
        result["score_adjusted"] = result["final_score"] != self.initial_score

        return result

    def _check_condition(self, condition: Dict, metric_data: Dict) -> bool:
        """检查单个条件是否触发"""
        condition_id = condition["id"]

        # 模拟数据查询（实际应从数据源获取）
        if condition_id not in metric_data:
            return False

        current_value = metric_data[condition_id]
        trigger = condition["trigger"]

        # 简单的触发逻辑（实际应更复杂）
        if "%" in trigger and ">" in trigger:
            threshold = float(trigger.split(">")[1].strip().rstrip("%"))
            return current_value > threshold

        return False

    def generate_html_section(self) -> str:
        """生成 HTML 报告段落"""

        html = f"""
        <section id="thesis-breaks-{self.ticker}" class="report-section">
            <h2>🚨 失效条件监控 (Thesis Breaks) - {self.ticker}</h2>

            <!-- Level 1 预警 -->
            <div class="thesis-break-container level-1">
                <h3 class="level-label">⚠️ Level 1: 预警条件（降低评分 -15%）</h3>
                <div class="conditions-grid">
        """

        for condition in self.config["level_1_warning"]["conditions"]:
            html += f"""
                    <div class="break-condition">
                        <div class="break-metric">{condition['metric']}</div>
                        <div class="break-details">
                            <p><span class="label">触发条件：</span>{condition['trigger']}</p>
                            <p><span class="label">数据来源：</span>{condition['data_source']}</p>
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
                            <p><span class="label">严重程度：</span>{condition.get('severity', 'MEDIUM')}</p>
                        </div>
                    </div>
            """

        html += """
                </div>
            </div>

            <!-- Level 2 认输 -->
            <div class="thesis-break-container level-2">
                <h3 class="level-label">🛑 Level 2: 认输条件（反转推荐）</h3>
                <div class="conditions-grid">
        """

        for condition in self.config["level_2_stop_loss"]["conditions"]:
            html += f"""
                    <div class="break-condition">
                        <div class="break-metric">{condition['metric']}</div>
                        <div class="break-details">
                            <p><span class="label">触发条件：</span>{condition['trigger']}</p>
                            <p><span class="label">数据来源：</span>{condition['data_source']}</p>
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
                            <p><span class="label">后续行动：</span>{condition.get('action', 'N/A')}</p>
                        </div>
                    </div>
            """

        html += """
                </div>
            </div>

            <!-- 监控仪表板 -->
            <div class="monitoring-dashboard">
                <h3>📊 实时监控状态</h3>
                <table class="monitoring-table">
                    <thead>
                        <tr>
                            <th>条件</th>
                            <th>触发阈值</th>
                            <th>当前值</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        # 添加监控行（示例）
        conditions = self.config.get("level_1_warning", {}).get("conditions", [])
        for condition in conditions:
            html += f"""
                        <tr>
                            <td>{condition['metric']}</td>
                            <td>{condition['trigger']}</td>
                            <td>{condition['current_status']}</td>
                            <td>✅ 安全</td>
                        </tr>
            """

        html += """
                    </tbody>
                </table>
            </div>
        </section>

        <style>
            #thesis-breaks-{ticker} {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                margin: 30px 0;
            }}

            .thesis-break-container {{
                margin: 20px 0;
                background: white;
                padding: 15px;
                border-radius: 6px;
            }}

            .level-1 {{
                border-left: 4px solid #ff9800;
            }}

            .level-2 {{
                border-left: 4px solid #f44336;
            }}

            .level-label {{
                margin-bottom: 15px;
                font-size: 16px;
                font-weight: 600;
            }}

            .conditions-grid {{
                display: grid;
                gap: 12px;
            }}

            .break-condition {{
                background: #fafafa;
                padding: 12px;
                border-radius: 4px;
                border-left: 3px solid #e0e0e0;
            }}

            .break-metric {{
                font-weight: 600;
                margin-bottom: 8px;
                color: #333;
            }}

            .break-details p {{
                margin: 4px 0;
                font-size: 13px;
            }}

            .label {{
                font-weight: 600;
                color: #666;
            }}

            .monitoring-table {{
                width: 100%;
                border-collapse: collapse;
                margin: 15px 0;
            }}

            .monitoring-table th {{
                background: #f5f5f5;
                padding: 10px;
                text-align: left;
                font-weight: 600;
                border-bottom: 2px solid #ddd;
            }}

            .monitoring-table td {{
                padding: 10px;
                border-bottom: 1px solid #eee;
            }}

            .monitoring-table tr:hover {{
                background: #f9f9f9;
            }}
        </style>
        """

        return html

    def save_to_json(self, filename: str = None) -> str:
        """保存监控配置到 JSON 文件"""

        if filename is None:
            filename = f"thesis_breaks_{self.ticker}.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
                "ticker": self.ticker,
                "config": self.config,
                "created_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

        return filename


# 使用示例
if __name__ == "__main__":
    # NVDA 监控
    nvda_monitor = ThesisBreakMonitor("NVDA", initial_score=8.52)

    # 模拟数据
    test_metrics = {
        "datacenter_revenue_decline": 2.5,  # 2.5% 增长（< 5% 下滑阈值，不触发）
        "competitor_threat": 0,
        "china_ban_risk": 35  # Polymarket 禁令概率 35%（< 60% 阈值，不触发）
    }

    result = nvda_monitor.check_all_conditions(test_metrics)
    print(f"✅ {result['ticker']} 检查完成")
    print(f"初始评分: {result['initial_score']}")
    print(f"最终评分: {result['final_score']}")
    print(f"警告数: {len(result['level_1_warnings'])}")
    print(f"认输数: {len(result['level_2_stops'])}")

    # 生成 HTML
    html = nvda_monitor.generate_html_section()
    print("\n✅ HTML 已生成")

    # 保存配置
    nvda_monitor.save_to_json()
    print("✅ 配置已保存")
