# 第三方组件声明

本项目在 `static/vendor/` 目录下打包了以下第三方前端库，用于离线运行，无需联网加载 CDN。
各组件版权归其原作者所有，遵循各自的许可证。列明如下：

| 文件 | 组件 | 许可证 | 主页 |
| --- | --- | --- | --- |
| `static/vendor/echarts.min.js` | Apache ECharts | Apache-2.0 | https://github.com/apache/echarts |
| `static/vendor/marked.min.js` | marked | MIT | https://github.com/markedjs/marked |
| `static/vendor/mermaid.min.js` | Mermaid | MIT | https://github.com/mermaid-js/mermaid |
| `static/vendor/purify.min.js` | DOMPurify | Apache-2.0 或 MPL-2.0（双许可） | https://github.com/cure53/DOMPurify |
| `static/vendor/qrcode.js` | qrcodejs | MIT | https://github.com/davidshimjs/qrcodejs |

说明：

- 各文件头部均保留其原始版权与许可证声明，请勿在再分发时移除。
- Mermaid 打包产物内含若干传递依赖（如 DOMPurify、js-yaml、lodash、cytoscape 等），
  其许可证信息保留在 `mermaid.min.js` 内的 "Bundled license information" 区块中。
- Apache-2.0 许可的组件要求保留版权与许可声明；MPL-2.0 与 MIT 组件同样要求保留声明。
  若需替换或升级这些库，请连同其许可证声明一并更新。

本项目自身代码以 [MIT License](LICENSE) 开源。
