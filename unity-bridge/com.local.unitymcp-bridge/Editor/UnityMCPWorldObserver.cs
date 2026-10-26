// UnityMCPWorldObserver.cs
// Phase 8: no-hallucination observation of NON-UI runtime subsystems
// (camera / entity / effect) for the verification layer.
//
// Unlike UnityMCPUIInspector (which reflection-walks the PROJECT_UI tree), the camera /
// entity / effect logical state does NOT live in scene GameObjects — it lives in
// Lua manager singletons (PROJECT_RUNTIME.global.PROJECT_CAMERA_SERVICE / PROJECT_RUNTIME.global.PROJECT_ENTITY_SERVICE) and plain C#
// managers (EffectManager). So observation runs FIXED, AUDITED Lua snippets through
// the existing DefaultLuaDoString seam: each snippet gathers the evidence-grade
// accessors and serializes a JSON string IN LUA (LuaEval flattens returns to text,
// so a structured table cannot cross the bridge — we build the JSON ourselves and
// return one string). The bridge parses that string back to a JToken.
//
// "No hallucination": the AI reads named fields from real queried runtime state; it
// does not author the Lua (the snippets are fixed here) and does not read source.
//
// The ONLY non-Lua path is ListEffects, which reflects EffectManager's private
// `PROJECT_EFFECT_COLLECTION` dictionary because the game exposes no [PROJECT_SCRIPT_EXPORT] enumerator.
//
// All snippets are PlayMode-only; the caller maps the PROJECT_LUA_VM-null throw to
// InvalidState 'lua_vm_not_running' (same as LuaEval).

using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;

namespace UnityMCP.Bridge
{
    internal static class UnityMCPWorldObserver
    {
        // -------------------------------------------------------------------------
        // Lua snippet builders. Each returns a chunk that `return`s a single JSON
        // string. A tiny JSON helper is prepended so the snippets stay readable and
        // numbers/strings/bools are escaped consistently.
        // -------------------------------------------------------------------------

        // Minimal Lua JSON-ish serializer prelude: jv(value) for scalars, used by the
        // snippets to build flat objects. Defined inline so no game dependency.
        private const string JsonPrelude =
            "local function _q(s) s=tostring(s); s=s:gsub('\\\\','\\\\\\\\'):gsub('\"','\\\\\"'):gsub('\\n','\\\\n'):gsub('\\r',''); return '\"'..s..'\"' end " +
            "local function _b(v) return v and 'true' or 'false' end " +
            "local function _n(v) if v==nil then return 'null' end local x=tonumber(v); if x==nil then return 'null' end return string.format('%.4f',x) end ";

        /// <summary>Camera snapshot. optionalTargetPos = {x,y,z} or null.</summary>
        internal static string BuildCameraSnapshotLua(double[] targetPos)
        {
            // All accessors verified [PROJECT_SCRIPT_EXPORT]/reachable (see plan Phase 6 facts).
            var sb = new System.Text.StringBuilder();
            sb.Append(JsonPrelude);
            sb.Append("local cm=PROJECT_RUNTIME.global.PROJECT_CAMERA_SERVICE; if not cm then return '{\"ok\":false,\"note\":\"PROJECT_CAMERA_SERVICE nil\"}' end ");
            sb.Append("local cs=PROJECT_RUNTIME.game and PROJECT_RUNTIME.game.camera ");
            sb.Append("local t=cm.PROJECT_CAMERA_INSTANCE and cm.PROJECT_CAMERA_INSTANCE.transform ");
            sb.Append("local p=t and t.position; local e=t and t.rotation.eulerAngles ");
            sb.Append("local top=nil; pcall(function() top=cm.PROJECT_CAMERA_STACK and cm.PROJECT_CAMERA_STACK.PROJECT_CAMERA_GROUP and cm.PROJECT_CAMERA_STACK.PROJECT_CAMERA_GROUP:PROJECT_ACTIVE_CAMERA_MODE() end) ");
            sb.Append("local modeName='nil'; pcall(function() modeName=top and top.cameraName or 'nil' end) ");
            sb.Append("local blending=false; pcall(function() blending=top and top.isBlending or false end) ");
            sb.Append("local zoom,zoomT=0,0 ");
            sb.Append("local okz=pcall(function() zoom=PROJECT_RUNTIME.game.camera.PROJECT_PLAYER_CAMERA_MODE.PROJECT_CAMERA_MODE.PROJECT_CAMERA_ZOOM.PROJECT_CURRENT_ZOOM; zoomT=PROJECT_RUNTIME.game.camera.PROJECT_PLAYER_CAMERA_MODE.PROJECT_CAMERA_MODE.PROJECT_CAMERA_ZOOM.PROJECT_TARGET_ZOOM end) ");
            sb.Append("local followId=0; pcall(function() local tp=cs and cs:getTargetPlayer(); followId=tp and (tp.actorId or 0) or 0 end) ");
            sb.Append("local shakeN=0; pcall(function() shakeN=#PROJECT_RUNTIME.global.PROJECT_CAMERA_SHAKE_SERVICE.activeShakes end) ");
            sb.Append("local fov=nil; pcall(function() fov=cm:PROJECT_CAMERA_FOV() end) ");
            sb.Append("local far=nil; pcall(function() far=cm:PROJECT_CAMERA_FAR_PLANE() end) ");
            // Optional adapter state may not be
            // xLua-exported in this build (then they read nil) — guard + keep nil so
            // the result honestly shows 'not readable' rather than a fake 0/false.
            sb.Append("local enabled=nil; pcall(function() enabled=cm.PROJECT_CAMERA_ENABLED end) ");
            sb.Append("local ts=nil; pcall(function() ts=cm.timeScale end) ");
            sb.Append("local focus=''; pcall(function() focus=cm.PROJECT_CAMERA_STACK.PROJECT_FOCUS_GROUP or '' end) ");
            if (targetPos != null && targetPos.Length == 3)
            {
                string tx = targetPos[0].ToString(System.Globalization.CultureInfo.InvariantCulture);
                string ty = targetPos[1].ToString(System.Globalization.CultureInfo.InvariantCulture);
                string tz = targetPos[2].ToString(System.Globalization.CultureInfo.InvariantCulture);
                sb.Append($"local inVp=false; pcall(function() inVp=cs:checkInViewport({{x={tx},y={ty},z={tz}}}) end) ");
                sb.Append($"local inVpC=false; pcall(function() inVpC=cs:checkInViewportCenter({{x={tx},y={ty},z={tz}}}) end) ");
            }
            else
            {
                sb.Append("local inVp=nil; local inVpC=nil ");
            }
            sb.Append("local parts={} ");
            sb.Append("parts[#parts+1]='\"ok\":true' ");
            sb.Append("parts[#parts+1]='\"position\":{\"x\":'.._n(p and p.x)..',\"y\":'.._n(p and p.y)..',\"z\":'.._n(p and p.z)..'}' ");
            sb.Append("parts[#parts+1]='\"rotationEuler\":{\"x\":'.._n(e and e.x)..',\"y\":'.._n(e and e.y)..',\"z\":'.._n(e and e.z)..'}' ");
            sb.Append("parts[#parts+1]='\"fov\":'.._n(fov) ");
            sb.Append("parts[#parts+1]='\"farPlane\":'.._n(far) ");
            sb.Append("parts[#parts+1]='\"activeModeName\":'.._q(modeName) ");
            sb.Append("parts[#parts+1]='\"isBlending\":'.._b(blending) ");
            sb.Append("parts[#parts+1]='\"PROJECT_CURRENT_ZOOM\":'.._n(zoom) ");
            sb.Append("parts[#parts+1]='\"zoomTarget\":'.._n(zoomT) ");
            sb.Append("parts[#parts+1]='\"followActorId\":'.._n(followId) ");
            sb.Append("parts[#parts+1]='\"activeShakeCount\":'.._n(shakeN) ");
            // emit nil-aware: a non-exported field stays null (not a fake false/0).
            sb.Append("parts[#parts+1]='\"worldCameraEnabled\":'..(enabled==nil and 'null' or _b(enabled)) ");
            sb.Append("parts[#parts+1]='\"timeScale\":'.._n(ts) ");
            sb.Append("parts[#parts+1]='\"focusGroup\":'.._q(focus) ");
            sb.Append("if inVp~=nil then parts[#parts+1]='\"inViewport\":'.._b(inVp) end ");
            sb.Append("if inVpC~=nil then parts[#parts+1]='\"inViewportCenter\":'.._b(inVpC) end ");
            sb.Append("return '{'..table.concat(parts,',')..'}' ");
            return sb.ToString();
        }

        /// <summary>Entity list. Filters are pre-escaped Lua string literals or nil.</summary>
        internal static string BuildListEntitiesLua(string actorTypeLit, string classNameLit,
            int? templateId, bool summary, int maxEntities)
        {
            var sb = new System.Text.StringBuilder();
            sb.Append(JsonPrelude);
            sb.Append("local em=PROJECT_RUNTIME.global.PROJECT_ENTITY_SERVICE; if not em then return '{\"ok\":false,\"note\":\"PROJECT_ENTITY_SERVICE nil\"}' end ");
            if (summary)
            {
                sb.Append("local c=em.profileEntityCount and em.profileEntityCount() or {} ");
                sb.Append("local parts={} for k,v in pairs(c) do parts[#parts+1]=_q(k)..':'.._n(v) end ");
                sb.Append("return '{\"ok\":true,\"summary\":{'..table.concat(parts,',')..'}}' ");
                return sb.ToString();
            }
            sb.Append("local rows={} local cnt=0 ");
            sb.Append($"local maxN={maxEntities} ");
            sb.Append("for id,e in pairs(em.getAllEntities()) do ");
            sb.Append("if cnt>=maxN then break end ");
            sb.Append("if e.isInited and not e.destroyed then ");
            sb.Append("local pass=true ");
            if (actorTypeLit != null) sb.Append($"if tostring(e.actorType)~={actorTypeLit} then pass=false end ");
            if (classNameLit != null) sb.Append($"if tostring(e.className)~={classNameLit} then pass=false end ");
            if (templateId.HasValue) sb.Append($"if e.templateId~={templateId.Value} then pass=false end ");
            sb.Append("if pass then ");
            sb.Append("local p=nil; pcall(function() p=e:getPosition() end) ");
            sb.Append("local alive=false; pcall(function() alive=e:isAlive() end) ");
            sb.Append("local yaw=0; pcall(function() yaw=e:getYaw() end) ");
            sb.Append("local dist=0; pcall(function() dist=e:getPlayerDistance() end) ");
            sb.Append("local row='{\"id\":'.._q(id)..',\"className\":'.._q(e.className)..',\"actorType\":'.._q(e.actorType)" +
                      "..',\"templateId\":'.._n(e.templateId)..',\"actorId\":'.._n(e.actorId)..',\"camp\":'.._n(e.camp)" +
                      "..',\"life\":'.._n(e.life)..',\"isAlive\":'.._b(alive)" +
                      "..',\"position\":{\"x\":'.._n(p and p.x)..',\"y\":'.._n(p and p.y)..',\"z\":'.._n(p and p.z)..'}'" +
                      "..',\"yaw\":'.._n(yaw)..',\"playerDistance\":'.._n(dist)" +
                      "..',\"active\":'.._b(e.active)..',\"visible\":'.._b(e.visible)..'}' ");
            sb.Append("rows[#rows+1]=row cnt=cnt+1 ");
            sb.Append("end end end ");
            sb.Append("return '{\"ok\":true,\"count\":'..#rows..',\"entities\":['..table.concat(rows,',')..']}' ");
            return sb.ToString();
        }

        /// <summary>Single entity full evidence. resolver = Lua expr that yields the entity.</summary>
        internal static string BuildObserveEntityLua(string resolverExpr)
        {
            var sb = new System.Text.StringBuilder();
            sb.Append(JsonPrelude);
            sb.Append($"local e={resolverExpr} ");
            sb.Append("if not e then return '{\"ok\":true,\"found\":false}' end ");
            sb.Append("local p=nil; pcall(function() p=e:getPosition() end) ");
            sb.Append("local lp=nil; pcall(function() lp=e:getLastPosition() end) ");
            sb.Append("local yaw=0; pcall(function() yaw=e:getYaw() end) ");
            sb.Append("local hp,maxhp,hpp=0,0,0; pcall(function() local a=e.PROJECT_COMBAT_STATE; hp=a:getHp(); maxhp=a:getMaxHp(); hpp=a:getHpPercent() end) ");
            sb.Append("local alive,dead,fake=false,false,false; pcall(function() alive=e:isAlive(); dead=e:isDead(); fake=e:isFakeDead() end) ");
            sb.Append("local inCombat=false; pcall(function() inCombat=e:isInCombat() end) ");
            sb.Append("local lockT,atkT=0,0; pcall(function() lockT=e:getLockedActorId(); atkT=e:getAttackTargetActorId() end) ");
            sb.Append("local cstate=-1; pcall(function() cstate=e.characterState end) ");
            sb.Append("local cstateName=''; pcall(function() local C=require('PROJECT_ADAPTER.StateNames'); local d=C[cstate]; cstateName=d and d.name or '' end) ");
            sb.Append("local animKey=''; pcall(function() animKey=tostring(e.animKey) end) ");
            sb.Append("local cfgName=''; pcall(function() local c=e:getConfigData() or {}; cfgName=tostring(c.name or c.modelText or '') end) ");
            sb.Append("local modelLoaded=false; pcall(function() modelLoaded=NotNil(e.PROJECT_ENTITY_MODEL) and (e.PROJECT_ENTITY_MODEL.isModelLoaded or (e.modelLoaded and e:modelLoaded())) and true or false end) ");
            sb.Append("local isP,isPet,isPup,isBoss=false,false,false,false; pcall(function() local U=require('PROJECT_ADAPTER.EntityQueries'); isP=U.isPlayer(e); isPet=U.isPet(e); isPup=U.isPuppet(e); isBoss=U.isBoss(e) end) ");
            sb.Append("local parts={} ");
            sb.Append("parts[#parts+1]='\"ok\":true' parts[#parts+1]='\"found\":true' ");
            sb.Append("parts[#parts+1]='\"id\":'.._q(e.id) parts[#parts+1]='\"className\":'.._q(e.className) ");
            sb.Append("parts[#parts+1]='\"actorId\":'.._n(e.actorId) parts[#parts+1]='\"templateId\":'.._n(e.templateId) ");
            sb.Append("parts[#parts+1]='\"position\":{\"x\":'.._n(p and p.x)..',\"y\":'.._n(p and p.y)..',\"z\":'.._n(p and p.z)..'}' ");
            sb.Append("parts[#parts+1]='\"lastPosition\":{\"x\":'.._n(lp and lp.x)..',\"y\":'.._n(lp and lp.y)..',\"z\":'.._n(lp and lp.z)..'}' ");
            sb.Append("parts[#parts+1]='\"yaw\":'.._n(yaw) ");
            sb.Append("parts[#parts+1]='\"hp\":'.._n(hp) parts[#parts+1]='\"maxHp\":'.._n(maxhp) parts[#parts+1]='\"hpPercent\":'.._n(hpp) ");
            sb.Append("parts[#parts+1]='\"isAlive\":'.._b(alive) parts[#parts+1]='\"isDead\":'.._b(dead) parts[#parts+1]='\"isFakeDead\":'.._b(fake) ");
            sb.Append("parts[#parts+1]='\"inCombat\":'.._b(inCombat) ");
            sb.Append("parts[#parts+1]='\"lockTarget\":'.._n(lockT) parts[#parts+1]='\"attackTarget\":'.._n(atkT) ");
            sb.Append("parts[#parts+1]='\"characterState\":'.._n(cstate) parts[#parts+1]='\"characterStateName\":'.._q(cstateName) ");
            sb.Append("parts[#parts+1]='\"animKey\":'.._q(animKey) ");
            sb.Append("parts[#parts+1]='\"configName\":'.._q(cfgName) ");
            sb.Append("parts[#parts+1]='\"modelLoaded\":'.._b(modelLoaded) ");
            sb.Append("parts[#parts+1]='\"isPlayer\":'.._b(isP) parts[#parts+1]='\"isPet\":'.._b(isPet) parts[#parts+1]='\"isPuppet\":'.._b(isPup) parts[#parts+1]='\"isBoss\":'.._b(isBoss) ");
            sb.Append("return '{'..table.concat(parts,',')..'}' ");
            return sb.ToString();
        }

        /// <summary>Single effect by generatorId+effectId.</summary>
        internal static string BuildObserveEffectLua(int generatorId, int effectId)
        {
            var sb = new System.Text.StringBuilder();
            sb.Append(JsonPrelude);
            sb.Append("local em=PROJECT_RUNTIME.global.PROJECT_EFFECT_SERVICE; if not em then return '{\"ok\":false,\"note\":\"PROJECT_EFFECT_SERVICE nil\"}' end ");
            sb.Append($"local it=nil; pcall(function() it=em:PROJECT_EFFECT_BY_ID({generatorId},{effectId}) end) ");
            sb.Append("if not it then return '{\"ok\":true,\"exists\":false}' end ");
            sb.Append("local playing=false; pcall(function() playing=it:IsPlaying() end) ");
            sb.Append("local active=false; pcall(function() active=it.PROJECT_EFFECT_OBJECT and it.PROJECT_EFFECT_OBJECT.activeInHierarchy or false end) ");
            sb.Append("local pc=0; pcall(function() if it.PROJECT_EFFECT_OBJECT then local arr=it.PROJECT_EFFECT_OBJECT:GetComponentsInChildren(typeof(CS.UnityEngine.ParticleSystem)); for i=0,arr.Length-1 do pc=pc+arr[i].particleCount end end end) ");
            sb.Append("local parts={} parts[#parts+1]='\"ok\":true' parts[#parts+1]='\"exists\":true' ");
            sb.Append("parts[#parts+1]='\"key\":'.._q(it.key or '') ");
            sb.Append("parts[#parts+1]='\"status\":'.._n(it.status) ");
            sb.Append("parts[#parts+1]='\"playing\":'.._b(playing) ");
            sb.Append("parts[#parts+1]='\"playedTime\":'.._n(it.playedTime) ");
            sb.Append("parts[#parts+1]='\"active\":'.._b(active) ");
            sb.Append("parts[#parts+1]='\"particleCount\":'.._n(pc) ");
            sb.Append("return '{'..table.concat(parts,',')..'}' ");
            return sb.ToString();
        }

        // -------------------------------------------------------------------------
        // CameraReflectionFields: read camera state that xLua does NOT export.
        //
        // The camera adapter exposes optional scale and enabled state through a
        // public C# *property*, but neither is [PROJECT_SCRIPT_EXPORT]-exported in this build, so
        // the Lua snapshot reads them as nil → null. Because the bridge runs as C#
        // INSIDE the Editor, it can reach the live CameraManager instance directly via
        // reflection (the same seam ListEffectsJson uses for PROJECT_FACADE.PROJECT_EFFECT_MANAGER)
        // and read the real values — this is the "if Lua can't read it, read it another
        // way" path. Returns a flat JSON object holding ONLY these reflected members;
        // the caller merges it into the Lua snapshot. Missing members are omitted (not
        // faked) so a genuinely-absent member stays honestly absent.
        //
        // PlayMode-only. Best-effort: any failure yields "{}" so the Lua snapshot is
        // still returned unchanged (reflection never breaks observation).
        // -------------------------------------------------------------------------

        internal static string CameraReflectionFieldsJson()
        {
            object cam = GetLiveCameraManager();
            if (cam == null) return "{}";

            Type t = cam.GetType();
            var parts = new List<string>();

            // timeScale: public float FIELD (not xLua-exported).
            if (TryReadFloat(t, cam, "timeScale", out float ts))
                parts.Add("\"timeScale\":" + ts.ToString("F4", System.Globalization.CultureInfo.InvariantCulture));

            // Project camera enabled-state adapter (not necessarily script-exported).
            if (TryReadBool(t, cam, "PROJECT_CAMERA_ENABLED", out bool wce))
                parts.Add("\"worldCameraEnabled\":" + (wce ? "true" : "false"));

            return "{" + string.Join(",", parts) + "}";
        }

        /// <summary>The live CameraManager instance via PROJECT_FACADE.PROJECT_CAMERA_MANAGER (static
        /// property), or null if the game is not running. Never throws.</summary>
        private static object GetLiveCameraManager()
        {
            try
            {
                Type facadeType = FindType("PROJECT_NAMESPACE.PROJECT_FACADE");
                if (facadeType == null) return null;
                PropertyInfo cmProp = facadeType.GetProperty("PROJECT_CAMERA_MANAGER",
                    BindingFlags.Public | BindingFlags.Static);
                return cmProp?.GetValue(null);
            }
            catch { return null; }
        }

        private static bool TryReadFloat(Type t, object o, string name, out float value)
        {
            value = 0f;
            object v = ReadMember(t, o, name);
            if (v == null) return false;
            if (v is float f) { value = f; return true; }
            if (v is double d) { value = (float)d; return true; }
            if (v is int i) { value = i; return true; }
            if (float.TryParse(v.ToString(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out float p)) { value = p; return true; }
            return false;
        }

        private static bool TryReadBool(Type t, object o, string name, out bool value)
        {
            value = false;
            object v = ReadMember(t, o, name);
            if (v is bool b) { value = b; return true; }
            return false;
        }

        // -------------------------------------------------------------------------
        // ListEffects: reflect EffectManager's private `PROJECT_EFFECT_COLLECTION` dictionary (no
        // [PROJECT_SCRIPT_EXPORT] enumerator exists). PlayMode-only — the manager singleton only
        // exists at runtime. Returns a JSON string (parsed by the caller).
        // -------------------------------------------------------------------------

        internal static string ListEffectsJson()
        {
            Type facadeType = FindType("PROJECT_NAMESPACE.PROJECT_FACADE");
            if (facadeType == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_NAMESPACE.PROJECT_FACADE not found — enter Play Mode.");
            PropertyInfo emProp = facadeType.GetProperty("PROJECT_EFFECT_MANAGER",
                BindingFlags.Public | BindingFlags.Static);
            object effMgr = emProp?.GetValue(null);
            if (effMgr == null)
                throw new UIInspectorNotReadyException(
                    "PROJECT_FACADE.PROJECT_EFFECT_MANAGER is null — the game is not running.");

            Type emType = effMgr.GetType();
            // private effect collection supplied by the project adapter
            FieldInfo gensField = emType.GetField("PROJECT_EFFECT_COLLECTION",
                BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Public);
            if (gensField == null)
                return "{\"ok\":false,\"note\":\"PROJECT_EFFECT_MANAGER.PROJECT_EFFECT_COLLECTION field not found (renamed?)\"}";
            var gens = gensField.GetValue(effMgr) as System.Collections.IDictionary;
            if (gens == null)
                return "{\"ok\":true,\"count\":0,\"effects\":[]}";

            var rows = new List<string>();
            foreach (System.Collections.DictionaryEntry kv in gens)
            {
                int generatorId = kv.Key is int gi ? gi : 0;
                object generator = kv.Value;
                if (generator == null) continue;
                MethodInfo getItems = generator.GetType().GetMethod("PROJECT_EFFECTS_BY_GROUP",
                    BindingFlags.Public | BindingFlags.Instance);
                var items = getItems?.Invoke(generator, null) as System.Collections.IEnumerable;
                if (items == null) continue;
                foreach (object item in items)
                {
                    if (item == null) continue;
                    Type it = item.GetType();
                    string key = ReadStr(it, item, "key");
                    int status = ReadInt(it, item, "status");
                    int effectId = ReadInt(it, item, "effectId");
                    double playedTime = ReadDouble(it, item, "playedTime");
                    int particleCount = ParticleCount(it, item);
                    rows.Add($"{{\"generatorId\":{generatorId},\"effectId\":{effectId}," +
                             $"\"key\":\"{EscapeJson(key)}\",\"status\":{status}," +
                             $"\"playedTime\":{playedTime.ToString("F4", System.Globalization.CultureInfo.InvariantCulture)}," +
                             $"\"particleCount\":{particleCount}}}");
                }
            }
            return $"{{\"ok\":true,\"count\":{rows.Count},\"effects\":[{string.Join(",", rows)}]}}";
        }

        private static int ParticleCount(Type itemType, object item)
        {
            try
            {
                // EffectItem.PROJECT_EFFECT_OBJECT (public GameObject [PROJECT_SCRIPT_EXPORT])
                var objMember = itemType.GetProperty("PROJECT_EFFECT_OBJECT", BindingFlags.Public | BindingFlags.Instance);
                object go = objMember != null ? objMember.GetValue(item)
                    : itemType.GetField("PROJECT_EFFECT_OBJECT", BindingFlags.Public | BindingFlags.Instance)?.GetValue(item);
                if (go is GameObject g && g != null)
                {
                    int sum = 0;
                    foreach (var ps in g.GetComponentsInChildren<ParticleSystem>())
                        sum += ps.particleCount;
                    return sum;
                }
            }
            catch { /* best-effort */ }
            return 0;
        }

        // -------------------------------------------------------------------------
        // small reflection readers + helpers
        // -------------------------------------------------------------------------

        private static Type FindType(string fullName)
        {
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = asm.GetType(fullName, throwOnError: false);
                if (t != null) return t;
            }
            return null;
        }

        private static object ReadMember(Type t, object o, string name)
        {
            const BindingFlags f = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            try { var p = t.GetProperty(name, f); if (p != null && p.CanRead) return p.GetValue(o); } catch { }
            try { var fi = t.GetField(name, f); if (fi != null) return fi.GetValue(o); } catch { }
            return null;
        }

        private static string ReadStr(Type t, object o, string name) => ReadMember(t, o, name)?.ToString() ?? "";
        private static int ReadInt(Type t, object o, string name)
        {
            object v = ReadMember(t, o, name);
            if (v is int i) return i;
            if (v is long l) return (int)l;
            if (v != null && int.TryParse(v.ToString(), out int p)) return p;
            return 0;
        }
        private static double ReadDouble(Type t, object o, string name)
        {
            object v = ReadMember(t, o, name);
            if (v is double d) return d;
            if (v is float fl) return fl;
            if (v != null && double.TryParse(v.ToString(), out double p)) return p;
            return 0;
        }

        private static string EscapeJson(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n").Replace("\r", "");
        }
    }
}
