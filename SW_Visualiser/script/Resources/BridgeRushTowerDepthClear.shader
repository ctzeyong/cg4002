// Used by TowerController so a tower is never hidden behind real-world objects (AR occlusion), while everything else
// still is. It draws an invisible copy of the tower BEFORE the tower itself and, over the tower's silhouette only,
// resets the depth buffer to "infinitely far". The AR camera background wrote the real world's depth there (that is how
// occlusion works), so the tower's normal draw then passes its depth test and is not cut by real objects. It writes no
// colour, and units and other virtual objects still sort against the tower normally.
Shader "BridgeRush/TowerDepthClear"
{
    SubShader
    {
        Tags { "RenderType" = "Opaque" "Queue" = "Geometry-10" }
        Pass
        {
            ZTest Always
            ZWrite On
            ColorMask 0
            Cull Off

            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            struct appdata { float4 vertex : POSITION; };
            struct v2f { float4 pos : SV_POSITION; };

            v2f vert(appdata v)
            {
                v2f o;
                o.pos = UnityObjectToClipPos(v.vertex);
                return o;
            }

            float frag(v2f i) : SV_Depth
            {
            #if defined(UNITY_REVERSED_Z)
                return 0.0; // far plane when the depth buffer is reversed
            #else
                return 1.0;
            #endif
            }
            ENDCG
        }
    }
}
