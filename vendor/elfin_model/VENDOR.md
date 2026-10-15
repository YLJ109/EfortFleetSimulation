# vendor/elfin_model

华沿（HUAYAN / ELFIN）协作臂**官方**模型描述文件的本地副本。放这里的原因：
`config/models/huayan_elfin.yaml` 的关节链数据必须有可回溯的原始出处，
不能只留一句「来自官方」，日后有人复核时要能直接 diff。

- 上游仓库：https://github.com/huayan-robotics/elfin_model
- 固定 commit：`84baf18d37eefa46b6f092c7fa1f105f81f70ecb`（2023-05-06）
- 抓取时间：2026-10-05
- 上游未声明 license（仓库无 LICENSE 文件）。**只在本项目内部用作运动学依据**，
  不要把它当作可再分发的资产打进对外交付物里；需要对外发布前先向厂商取得授权。

## 本目录内容

```
urdf/elfin3.urdf.xacro      ← 六款机型的关节链原文
urdf/elfin5.urdf.xacro         （单位 m / rad，含 <limit> 关节软限位）
urdf/elfin5_l.urdf.xacro
urdf/elfin10.urdf.xacro
urdf/elfin10_l.urdf.xacro
urdf/elfin15.urdf.xacro
README_upstream.md          ← 上游 README 原文（含 485/总线末端两套模型的高度表）
```

来源路径统一是 `urdf/ROS2/ethercat/<model>.urdf.xacro`。
上游另有 `urdf/ROS2/485/` 与 `urdf/ROS1/*`、以及 39MB 的 `model/**/*.STL` 网格。

## 2026-10-06 补拉：elfin5（总线末端）官方 STL

按 `config/models/huayan_elfin.yaml` 的 variant（standing_height 1176.5 mm 与
上游 README 的 ethercat E05 一行对上）只取了**在用那一台**的 8 个网格：

```
model/ethercat/elfin5/elfin_base.STL        701,684 B   14,032 面
                              elfin_link1..6.STL        合计 ~11.08 MB / 221,550 面
                              elfin_end_link.STL      6,457,284 B  129,144 面
```

- 取法：`raw.githubusercontent.com/.../<固定 commit>/model/ethercat/elfin5/<file>`，
  逐个校验**不是 LFS 指针**（前 64 字节无 `version https://git-lfs`），是二进制 STL 原件。
- **没有转 GLB**：本机无 trimesh/assimp/pygltflib，手写转换等于在厂商几何和屏幕之间
  插一个没人复核过的步骤。前端用 three 的 `STLLoader` 直接吃原件，1:1 可证明。
- 已核对单位与尺度：原件是**米**，`elfin_link2` 包围盒 0.118×0.488×0.097 m，
  与 xacro 里 L2=0.380 / L3=0.420 的臂长量级一致。
- 前端副本在 `frontend/public/models/elfin5/`（18 MB），路径记在
  `config/models/huayan_elfin.yaml` 的 `mesh.dir` / `mesh.files`。
- `elfin_end_link.STL` 与 `elfin_link6.STL` 是同一份几何（三角面数完全相同），
  前端只挂 link6，避免一个法兰画两遍。
- 性能：一台约 23.5 万面，12 台全用实测 9~10 FPS，因此**只有被选中那台**用原件，
  其余用关节链骨架（见 `src/three/fleet.js` 的 `setReal`）。要 12 台全原件需先简化网格。

## 与机械说明书的交叉验证

| 机型 | xacro 里 L2+L3 | 说明书工作范围 |
| --- | --- | --- |
| E03 | 0.266+0.324 = 0.590 m | 590 mm |
| E05 | 0.380+0.420 = 0.800 m | 800 mm |
| E10 | 0.480+0.520 = 1.000 m | 1000 mm |

两条独立来源对得上，才把这套数据写进 `config/models/huayan_elfin.yaml`。
说明书《中文_UL_Elfin机械说明书_(ver3.0)》第 5 章另写「关节范围 ±360°」，
与 xacro 的 `<limit>`（J2 ±134.6° 等）冲突，取 xacro —— 理由记在配置文件里。
