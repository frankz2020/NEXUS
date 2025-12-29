# School Brand Colors - Configuration Guide

## 📋 统一颜色配置

所有学校品牌颜色现已统一管理，确保在整个系统中保持一致。

## 🎨 颜色定义

### 主配置文件: `app.py`

```python
SCHOOLS = {
    "NYU": {"name": "New York University", "color": "#57068c", "folder": "NYU_Weekly"},
    "USC": {"name": "University of Southern California", "color": "#990000", "folder": "USC_Weekly"},
    "EMORY": {"name": "Emory University", "color": "#222c66", "folder": "EMORY_Weekly"},
    "UCD": {"name": "UC Davis", "color": "#022851", "folder": "UCD_Weekly"},
    "UBC": {"name": "University of British Columbia", "color": "#002145", "folder": "UBC_Weekly"},
    "EDINBURGH": {"name": "University of Edinburgh", "color": "#041e42", "folder": "EDIN_Weekly"},
}
```

### CSS变量定义: `templates/school-colors.css`

```css
:root {
    --school-nyu: #57068c;       /* New York University - Purple */
    --school-usc: #990000;       /* USC - Cardinal Red */
    --school-emory: #222c66;     /* Emory - Navy Blue */
    --school-ucd: #022851;       /* UC Davis - Blue */
    --school-ubc: #002145;       /* UBC - Navy */
    --school-edin: #041e42;      /* Edinburgh - Dark Blue */
    --school-default: #4a4a4a;   /* Default - Gray */
}
```

## 📊 学校颜色一览表

| 学校 | 代码 | 颜色 | 色值 | 文件夹 |
|-----|------|-----|------|--------|
| 🟣 New York University | `nyu` | 紫色 | `#57068c` | NYU_Weekly |
| 🔴 University of Southern California | `usc` | 深红 | `#990000` | USC_Weekly |
| 🔵 Emory University | `emory` | 深蓝 | `#222c66` | EMORY_Weekly |
| 🔵 UC Davis | `ucd` | 蓝色 | `#022851` | UCD_Weekly |
| 🔵 University of British Columbia | `ubc` | 深蓝 | `#002145` | UBC_Weekly |
| 🔵 University of Edinburgh | `edin` | 藏青 | `#041e42` | EDIN_Weekly |

## 🛠️ 使用指南

### 1. HTML/CSS 中使用

```css
/* ✅ 正确 - 使用 CSS 变量 */
.my-element {
    background: var(--school-nyu);
    border-color: var(--school-usc);
}

/* ❌ 错误 - 不要硬编码颜色 */
.my-element {
    background: #57068c;
}
```

### 2. 动态类名

使用预定义的工具类：

```html
<!-- 文字颜色 -->
<span class="school-nyu">NYU 紫色文字</span>

<!-- 背景颜色 -->
<div class="bg-school-usc">USC 红色背景</div>

<!-- 边框颜色 -->
<div class="border-school-edin">Edinburgh 边框</div>
```

### 3. JavaScript 中使用

```javascript
// ✅ 从 CSS 变量读取
const nyuColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--school-nyu');

// ✅ 或使用数据映射
const schoolColors = {
    'NYU': 'var(--school-nyu)',
    'USC': 'var(--school-usc)',
    // ...
};
```

## 🔄 修改颜色流程

如需修改学校品牌颜色，**必须**同时更新以下文件：

1. **`app.py`** - 后端配置
   ```python
   SCHOOLS = {
       "NYU": {"color": "#新颜色"},
       # ...
   }
   ```

2. **`templates/index.html`** - 主界面 CSS 变量
   ```css
   :root {
       --school-nyu: #新颜色;
   }
   ```

3. **`templates/guide.html`** - 操作指南 CSS 变量
   ```css
   :root {
       --school-nyu: #新颜色;
   }
   ```

4. **`templates/school-colors.css`** - 独立 CSS 文件（可选，备用）

5. 更新颜色值说明文字（如有显示十六进制值的地方）

## ✅ 颜色一致性检查清单

- [ ] `app.py` SCHOOLS 字典
- [ ] `templates/index.html` :root 变量
- [ ] `templates/guide.html` :root 变量
- [ ] 所有硬编码颜色已替换为 CSS 变量
- [ ] 文档中的颜色值已更新
- [ ] 测试所有学校在界面上的显示效果

## 📝 注意事项

1. **命名约定**: 后端使用 `EDINBURGH`，前端 CSS 使用 `edin`
2. **颜色格式**: 统一使用小写六位十六进制格式 `#rrggbb`
3. **默认颜色**: 未知学校使用 `--school-default: #4a4a4a`
4. **文字颜色**: 深色背景上默认使用白色文字

## 🔍 相关文件位置

```
NEXUS-4/
├── app.py                          # 后端颜色配置
├── templates/
│   ├── index.html                  # 主界面（含CSS变量）
│   ├── guide.html                  # 操作指南（含CSS变量）
│   └── school-colors.css           # 独立CSS文件（可选）
├── SCHOOL_COLORS.md                # 本文档
└── scripts/
    ├── text_to_image.py            # 图片生成使用的颜色
    └── generate_sources_image.py   # 来源图生成使用的颜色
```

---

**最后更新**: 2025-12-29  
**维护者**: NEXUS-4 Team

