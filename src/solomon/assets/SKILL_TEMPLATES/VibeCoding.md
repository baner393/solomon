# VibeCoding 笔记模板

> 设计参考：ytkn「General knowledge note」（a balanced note with takeaways, applications, and limits）+
> kepano 资源属性化（frontmatter 记 topics/style）;Zettelkasten lead 核心句
> 适用：Vibe Coding（氛围编程/提示词驱动开发）、UI 设计、界面审美、风格积累类视频
> 目标：这类视频教的不是语法而是「怎么描述需求给 AI / 怎么判断设计好不好」——
> 核心资产 = 可复用 Prompt + 设计决策 + 审美标准

## 笔记结构

```markdown
# {视频标题}

> [!Note] 核心句（lead）
> 这堂课建立的能力：{描述需求的技巧 / 判断设计好坏的标准}。能直接复用：{Prompt / 原则}。

> **课程来源**：{平台} · {课程名} · {主讲老师}
> **风格主题**：{minimalist / glassmorphism / ...}
> **标签**：{type/tool 标签}

---

## 一、核心观点（Takeaways）
- 关键的 1-3 个判断标准 / 思维方式

## 二、设计原则（Principles）
### 原则1：{原则名}
（为什么对 + 怎么看出来的）
![[filename.jpg]]
（图示：这张图示范了该原则的什么）

### 原则2：{原则名}
...

## 三、可复用 Prompt 模板
```text
你是{角色}，请{任务}。
要求：{关键约束1}、{关键约束2}。
```
（适用场景 + 为什么这样写）

## 四、应用与边界（Applications & Limits）
**可用在：** ...

**限制/不适用：** ...

## 五、建立自己的风格库
- 本篇值得收进风格库的样式/片段/关键词
```

## 写作要点

1. **Prompt 原样可复制**：有效 Prompt 完整整理进模板并标注用途——VibeCoding 笔记的核心资产（ytkn「能用」导向）
2. **设计决策 > 效果展示**：不只记"做成什么样"，要记"为什么这样做、判断依据"
3. **「应用与边界」成节**：哪些场景能用、哪些不能（ytkn General knowledge 的 limits 维度）
4. **好 vs 坏对比要记**：两边都记，配图标注差异
5. **建「风格库」清单**：把可复用的样式/片段单独列出，方便以后积累成库（kepano 资源化思路）
6. **配图用 wiki embed**：`![[filename.jpg]]`，图后必须有教学说明

## 示例模板

【Prompt】{用途名}（关键词）

```text
可复制的 Prompt 原文
```

**适用场景：** ...
**为什么这样写：** ...

【好 vs 坏】

![[keyframes_XXX_HH:MM:SS.jpg]]
好的设计：... 因为：... | 坏的设计：... 因为：...