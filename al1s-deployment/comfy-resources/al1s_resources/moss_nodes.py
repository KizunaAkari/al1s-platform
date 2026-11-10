def build_moss_nodes(registry, native_nodes, *, common, schema, check, cached, asset_hash, cache_key):
    result = {}

    native = native_nodes.get("UNETLoader")
    if native is not None:
        class UNet(common):
            RETURN_TYPES = ("MODEL",)
            @classmethod
            def INPUT_TYPES(cls): return schema(native)
            def process(self, unet_name, weight_dtype, al1s_prompt_id):
                check(al1s_prompt_id)
                if weight_dtype != "default":
                    raise ValueError("resource_model_type_invalid")
                digest = asset_hash(al1s_prompt_id, "diffusion_models", unet_name)
                return cached(al1s_prompt_id, cache_key("unet", digest, weight_dtype),
                              lambda: native().load_unet(unet_name, weight_dtype))
        result["AL1SUNETLoader"] = UNet

    native_clip = native_nodes.get("CLIPLoader")
    if native_clip is not None:
        class Clip(common):
            RETURN_TYPES = ("CLIP",)
            @classmethod
            def INPUT_TYPES(cls): return schema(native_clip)
            def process(self, clip_name, type, al1s_prompt_id, device="default"):
                check(al1s_prompt_id)
                if type not in {"moss_image", "qwen_image"} or device != "default":
                    raise ValueError("resource_model_type_invalid")
                digest = asset_hash(al1s_prompt_id, "text_encoders", clip_name)
                return cached(al1s_prompt_id, cache_key("clip", digest, type, device),
                              lambda: native_clip().load_clip(clip_name, type, device))
        result["AL1SCLIPLoader"] = Clip

    native_vae = native_nodes.get("VAELoader")
    if native_vae is not None:
        class Vae(common):
            RETURN_TYPES = ("VAE",)
            @classmethod
            def INPUT_TYPES(cls): return schema(native_vae)
            def process(self, vae_name, al1s_prompt_id):
                check(al1s_prompt_id)
                digest = asset_hash(al1s_prompt_id, "vae", vae_name)
                return cached(al1s_prompt_id, cache_key("vae", digest), lambda: native_vae().load_vae(vae_name))
        result["AL1SVAELoader"] = Vae

    native_lora = native_nodes.get("LoraLoaderModelOnly")
    if native_lora is not None:
        class Lora(common):
            RETURN_TYPES = ("MODEL",)
            @classmethod
            def INPUT_TYPES(cls): return schema(native_lora)
            def process(self, model, lora_name, strength_model, al1s_prompt_id):
                check(al1s_prompt_id)
                digest = asset_hash(al1s_prompt_id, "loras", lora_name)
                parent = registry.key_for_value(model)
                key = cache_key("lora_model", digest, parent, strength_model)
                return cached(al1s_prompt_id, key,
                              lambda: native_lora().load_lora_model_only(model, lora_name, strength_model), (parent,))
        result["AL1SLoraLoaderModelOnly"] = Lora

    native_skip = native_nodes.get("SkipLayerGuidanceDiTSimple")
    if native_skip is not None:
        class Skip(common):
            RETURN_TYPES = ("MODEL",)
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"model": ("MODEL",), "double_layers": ("STRING",),
                    "single_layers": ("STRING",), "start_percent": ("FLOAT", {"min": 0, "max": 1}),
                    "end_percent": ("FLOAT", {"min": 0, "max": 1}), "al1s_prompt_id": ("STRING",)}}
            def process(self, model, double_layers, single_layers, start_percent, end_percent, al1s_prompt_id):
                check(al1s_prompt_id)
                parent = registry.key_for_value(model)
                key = cache_key("skip_layers", parent, double_layers, single_layers, start_percent, end_percent)
                return cached(al1s_prompt_id, key, lambda: tuple(native_skip.execute(model=model,
                    double_layers=double_layers, single_layers=single_layers,
                    start_percent=start_percent, end_percent=end_percent).result), (parent,))
        result["AL1SSkipLayerGuidanceDiTSimple"] = Skip

    native_tsr = native_nodes.get("TemporalScoreRescaling")
    if native_tsr is not None:
        class Tsr(common):
            RETURN_TYPES = ("MODEL",)
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"model": ("MODEL",), "tsr_k": ("FLOAT", {"min": 0.01, "max": 100}),
                    "tsr_sigma": ("FLOAT", {"min": 0.01, "max": 100}), "al1s_prompt_id": ("STRING",)}}
            def process(self, model, tsr_k, tsr_sigma, al1s_prompt_id):
                check(al1s_prompt_id)
                parent = registry.key_for_value(model)
                key = cache_key("tsr", parent, tsr_k, tsr_sigma)
                return cached(al1s_prompt_id, key,
                              lambda: tuple(native_tsr.execute(model, tsr_k, tsr_sigma).result), (parent,))
        result["AL1STemporalScoreRescaling"] = Tsr
    return result
