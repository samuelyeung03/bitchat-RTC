package com.bitchat.android.ui

import android.view.TextureView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

@Composable
fun VideoCallView(
    modifier: Modifier = Modifier,
    onTextureViewReady: (TextureView) -> Unit
) {
    val colorScheme = MaterialTheme.colorScheme

    Box(
        modifier = modifier
            .fillMaxWidth()
            .aspectRatio(4f / 3f)
            .background(colorScheme.surfaceVariant)
    ) {
        AndroidView(
            factory = { context ->
                TextureView(context).apply {
                    onTextureViewReady(this)
                }
            },
            modifier = Modifier.fillMaxSize()
        )
    }
}
