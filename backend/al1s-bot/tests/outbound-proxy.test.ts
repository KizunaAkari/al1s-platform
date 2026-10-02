import { describe, expect, it, vi } from "vitest";
const bootstrap=vi.hoisted(()=>vi.fn());
vi.mock("global-agent",()=>({bootstrap}));
vi.mock("undici",()=>({ProxyAgent:class { constructor(public uri:string){} }}));
import { outboundProxy } from "../src/outbound-proxy.js";
describe("explicit bot proxy",()=>{
  it("keeps direct mode when not configured",()=>{
    expect(outboundProxy({})).toEqual({});expect(bootstrap).not.toHaveBeenCalled();
  });
  it.each(["file:///tmp/x","http://user:secret@host:1","http://host/?token=x","not a url"])("rejects an unsafe address without echoing it",value=>{
    expect(()=>outboundProxy({AL1S_BOT_HTTP_PROXY:value})).toThrow("invalid_bot_proxy");
  });
  it("sets REST and WebSocket proxies while bypassing internal endpoints",()=>{
    const result=outboundProxy({AL1S_BOT_HTTP_PROXY:"http://host.docker.internal:7897"});
    expect(result.rest?.agent).toBeDefined();
    expect(bootstrap).toHaveBeenCalledWith({environmentVariableNamespace:"AL1S_BOT_"});
    expect(process.env.AL1S_BOT_NO_PROXY).toContain("al1s-llbot");
  });
});
