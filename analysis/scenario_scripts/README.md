# 场景测试脚本

五类场景的可视化留证是这么产生的：

1. 启动 MVP：`cd src && python run.py`（默认 8080，本次用 8097）；
2. 起一个带远程调试的浏览器：
   `msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<临时目录> about:blank`
3. 用 `repo/cdp.mjs`（Chrome DevTools Protocol 小工具）打开页面，执行本目录下的脚本：
   - `boundary.js` / `adversarial.js` / `permission.js` / `fault.js`
   每个脚本会调用接口、核对预期，并把「编号 / 用例 / 请求 / 期望 / 实际 / 结果」渲染成一张报告表；
4. 截图保存到 `docs/test-images/`，再由 `analysis/build_scene_docs.py` 组装成五份 docx。

正常场景不跑脚本，直接在界面上操作并截图（问答、批次、Agent 确认门、完成态）。

> 脚本里的断言与《测试方案》第 3 章一一对应；改接口后重跑脚本即可刷新留证。
