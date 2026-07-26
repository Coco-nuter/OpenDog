package com.example.opendog.event

data class PageSnapshot(
    val packageName: String,
    val className: String,
    val windowId: Int?,
    val title: String,
    val localText: String = "",
    val focusId: String = "$packageName/$className"
)
