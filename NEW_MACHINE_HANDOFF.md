# plow-whip 新电脑接力

项目位置：

```bash
cd ~/work/plow-whip多AI协作机制/plow-whip
```

新电脑首次接力：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
plow-whip configure --projects-dir ~/work/plow-whip多AI协作机制 --agents planner builder reviewer
plow-whip agent set planner --role "PM / Architect" --assignment "Break work into tasks and decide next action"
plow-whip agent set builder --role "Code Owner" --assignment "Implement scoped tasks and report checks"
plow-whip agent set reviewer --role "Reviewer" --assignment "Review implementation and risks"
pytest
```

配置模板在 `config.example.json`。复制其内容到 `~/.plow-whip/config.json` 也可以，但推荐用上面的 `plow-whip configure` 自动生成真实路径。

日常继续开发：

```bash
cd ~/work/plow-whip多AI协作机制/plow-whip
source .venv/bin/activate
pytest
```

常用命令：

```bash
plow-whip --help
plow-whip --project MyProject init
plow-whip --project MyProject status
plow-whip whip
```

本机状态：

- `~/.plow-whip/config.json` 的 `projects_dir` 应指向 `~/work/plow-whip多AI协作机制`。
- 仓库只提交 `config.example.json`，不要提交自己的 `~/.plow-whip/config.json`。
- 当前项目状态机在 `collab/AGENT_STATE.json`。
- 当前项目留言板在 `collab/AGENT_COMMS.md`。

接力重点：

- `plow_whip/` 是源码。
- `tests/` 是最小回归检查。
- `docs/` 存架构、概念和 Qoder/Codex 协作资料。
- `.DS_Store`、`__pycache__`、`*.egg-info` 是本地生成物，已被 `.gitignore` 忽略。
