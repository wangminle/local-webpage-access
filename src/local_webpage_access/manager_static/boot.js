/* Local Webpage Access Manager — Vue 3 启动入口（DEV-046）。

唯一通过 importmap 以 ESM 引入 Vue 的文件（``import { createApp } from "vue"``），
把 vue 交给 app.js 的工厂挂载到 ``#app``。helpers.js / app.js 为普通脚本
（便于 Node 单测），本文件仅作浏览器侧 3 行胶水，无需单测。 */
import { createApp } from "vue";

var handle = window.LWA.createManagerApp({ createApp: createApp });
handle.mount("#app");
