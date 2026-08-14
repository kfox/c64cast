import { mount } from "svelte";

import App from "./App.svelte";
import "./app.css";

const target = document.getElementById("app");
if (!target) throw new Error("no #app element to mount into");

export default mount(App, { target });
