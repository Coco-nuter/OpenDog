package com.example.opendog.event

data class PageSnapshot(
    val packageName: String,
    val className: String,
    val windowId: Int?,
    val title: String,
    val localText: String = "",
    val focusId: String = "$packageName/$className",
    val visibleNodeCount: Int = 0,
    val textNodeCount: Int = 0,
    val hasSurfaceContent: Boolean = false,
    val captureMethod: String = "accessibility"
)
