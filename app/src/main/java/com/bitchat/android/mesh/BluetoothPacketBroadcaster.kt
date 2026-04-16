package com.bitchat.android.mesh

import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattServer
import android.util.Log
import com.bitchat.android.model.BitchatFilePacket
import com.bitchat.android.model.BitchatMessageType
import com.bitchat.android.model.NoisePayloadType
import com.bitchat.android.protocol.SpecialRecipients
import com.bitchat.android.model.RoutedPacket
import com.bitchat.android.protocol.BitchatPacket
import com.bitchat.android.protocol.MessageType
import com.bitchat.android.util.toHexString
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.Job
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.channels.actor
import java.util.UUID

/**
 * Handles packet broadcasting to connected devices using actor pattern for serialization
 * 
 * In Bluetooth Low Energy (BLE):
 *
 * Peripheral (server):
 * Advertises.
 * Accepts connections.
 * Hosts a GATT server.
 * Remote devices read/write/subscribe to characteristics.
 *
 *  Central (client):
 * Scans.
 * Initiates connections.
 * Hosts a GATT client.
 * Reads/writes to the peripheral’s characteristics.
 */
class BluetoothPacketBroadcaster(
    private val connectionScope: CoroutineScope,
    private val connectionTracker: BluetoothConnectionTracker,
    private val fragmentManager: FragmentManager?
) {
    private val debugManager by lazy { try { com.bitchat.android.ui.debug.DebugSettingsManager.getInstance() } catch (e: Exception) { null } }
    companion object {
        private const val TAG = "BluetoothPacketBroadcaster"
        private const val CLEANUP_DELAY = com.bitchat.android.util.AppConstants.Mesh.BROADCAST_CLEANUP_DELAY_MS
    }
    // Optional nickname resolver injected by higher layer (peerID -> nickname?)
    private var nicknameResolver: ((String) -> String?)? = null

    fun setNicknameResolver(resolver: (String) -> String?) {
        nicknameResolver = resolver
    }

    // Flow control hook: called before each client (GATT write) fragment.
    // Set to BluetoothGattClientManager::awaitWritePermit by BluetoothConnectionManager.
    private var clientWriteAwaiter: (suspend (deviceAddress: String) -> Unit)? = null

    fun setClientWriteAwaiter(awaiter: suspend (deviceAddress: String) -> Unit) {
        clientWriteAwaiter = awaiter
    }
    
    /**
     * Debug logging helper - can be easily removed/disabled for production
     */
    private fun logPacketRelay(
        typeName: String,
        senderPeerID: String,
        senderNick: String?,
        incomingPeer: String?,
        incomingAddr: String?,
        toPeer: String?,
        toDeviceAddress: String,
        ttl: UByte
    ) {
        try {
            val fromNick = incomingPeer?.let { nicknameResolver?.invoke(it) }
            val toNick = toPeer?.let { nicknameResolver?.invoke(it) }
            val isRelay = (incomingAddr != null || incomingPeer != null)
            
            com.bitchat.android.ui.debug.DebugSettingsManager.getInstance().logPacketRelayDetailed(
                packetType = typeName,
                senderPeerID = senderPeerID,
                senderNickname = senderNick,
                fromPeerID = incomingPeer,
                fromNickname = fromNick,
                fromDeviceAddress = incomingAddr,
                toPeerID = toPeer,
                toNickname = toNick,
                toDeviceAddress = toDeviceAddress,
                ttl = ttl,
                isRelay = isRelay
            )
        } catch (_: Exception) { 
            // Silently ignore debug logging failures
        }
    }
    
    // Data class to hold broadcast request information
    private data class BroadcastRequest(
        val routed: RoutedPacket,
        val gattServer: BluetoothGattServer?,
        val characteristic: BluetoothGattCharacteristic?
    )
    
    // Actor scope for the broadcaster
    private val broadcasterScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val transferJobs = ConcurrentHashMap<String, Job>()
    
    // SERIALIZATION: Actor to serialize all broadcast operations
    @OptIn(kotlinx.coroutines.ObsoleteCoroutinesApi::class)
    private val broadcasterActor = broadcasterScope.actor<BroadcastRequest>(
        capacity = Channel.UNLIMITED
    ) {
        Log.d(TAG, "🎭 Created packet broadcaster actor")
        try {
            for (request in channel) {
                broadcastSinglePacketInternal(request.routed, request.gattServer, request.characteristic)
            }
        } finally {
            Log.d(TAG, "🎭 Packet broadcaster actor terminated")
        }
    }
    
    fun broadcastPacket(
        routed: RoutedPacket,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?
    ) {
        val packet = routed.packet
        val isFile = packet.type == MessageType.FILE_TRANSFER.value
        if (isFile) {
            Log.d(TAG, "📤 Broadcasting FILE_TRANSFER: ${packet.payload.size} bytes")
        }
        // Prefer caller-provided transferId (e.g., for encrypted media), else derive for FILE_TRANSFER
        val transferId = routed.transferId ?: (if (isFile) sha256Hex(packet.payload) else null)
        // Check if we need to fragment
        if (fragmentManager != null) {
            val fragments = try {
                fragmentManager.createFragments(packet)
            } catch (e: Exception) {
                Log.e(TAG, "❌ Fragment creation failed: ${e.message}", e)
                if (isFile) {
                    Log.e(TAG, "❌ File fragmentation failed for ${packet.payload.size} byte file")
                }
                return
            }
            if (fragments.size > 1) {
                if (isFile) {
                    Log.d(TAG, "🔀 File needs ${fragments.size} fragments")
                }
                Log.d(TAG, "Fragmenting packet into ${fragments.size} fragments")
                if (transferId != null) {
                    TransferProgressManager.start(transferId, fragments.size)
                }
                val job = connectionScope.launch {
                    var sent = 0
                    // Determine client-side targets (we are the GATT central writing to them).
                    // We need their device address to gate flow control per-device.
                    val clientTargets = connectionTracker.getConnectedDevices().values
                        .filter { it.isClient && it.gatt != null && it.characteristic != null }

                    if (clientTargets.isEmpty()) {
                        // Server-notify path: no write-callback flow control needed.
                        fragments.forEach { fragment ->
                            if (!isActive) return@launch
                            if (transferId != null && transferJobs[transferId]?.isCancelled == true) return@launch
                            broadcastSinglePacket(RoutedPacket(fragment, transferId = transferId), gattServer, characteristic)
                            delay(20)
                            if (transferId != null) {
                                sent += 1
                                TransferProgressManager.progress(transferId, sent, fragments.size)
                                if (sent == fragments.size) TransferProgressManager.complete(transferId, fragments.size)
                            }
                        }
                    } else {
                        // Client-write path: await write callback before sending next fragment.
                        val awaiter = clientWriteAwaiter
                        fragments.forEach { fragment ->
                            if (!isActive) return@launch
                            if (transferId != null && transferJobs[transferId]?.isCancelled == true) return@launch
                            // Gate each client device before writing.
                            if (awaiter != null) {
                                clientTargets.forEach { conn ->
                                    try { awaiter(conn.device.address) } catch (_: Exception) { }
                                }
                            }
                            broadcastSinglePacket(RoutedPacket(fragment, transferId = transferId), gattServer, characteristic)
                            if (transferId != null) {
                                sent += 1
                                TransferProgressManager.progress(transferId, sent, fragments.size)
                                if (sent == fragments.size) TransferProgressManager.complete(transferId, fragments.size)
                            }
                        }
                    }
                }
                if (transferId != null) {
                    transferJobs[transferId] = job
                    job.invokeOnCompletion { transferJobs.remove(transferId) }
                }
                return
            }
        }
        
        // Send single packet if no fragmentation needed
        if (transferId != null) {
            TransferProgressManager.start(transferId, 1)
        }
        broadcastSinglePacket(routed, gattServer, characteristic)
        if (transferId != null) {
            TransferProgressManager.progress(transferId, 1, 1)
            TransferProgressManager.complete(transferId, 1)
        }
    }

    fun cancelTransfer(transferId: String): Boolean {
        val job = transferJobs.remove(transferId) ?: return false
        job.cancel()
        return true
    }

    /**
     * Send a packet to a specific peer only, without broadcasting.
     * Returns true if a direct path was found and used.
     */
    fun sendPacketToPeer(
        routed: RoutedPacket,
        targetPeerID: String,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?
    ): Boolean {
        val packet = routed.packet
        val isFile  = packet.type == MessageType.FILE_TRANSFER.value
        val isVideo = packet.type == MessageType.VIDEO.value
        val transferId = routed.transferId ?: (if (isFile) sha256Hex(packet.payload) else null)

        // Fragment large packets (video frames always exceed 469 B)
        if (fragmentManager != null) {
            val fragments = try { fragmentManager.createFragments(packet) }
            catch (e: Exception) { Log.e(TAG, "Fragment failed: ${e.message}"); return false }

            if (fragments.size > 1) {
                // Resolve the client connection once, before launching the coroutine.
                val clientConn = connectionTracker.getConnectedDevices().values.firstOrNull {
                    connectionTracker.addressPeerMap[it.device.address] == targetPeerID
                        && it.characteristic != null && it.isClient
                }
                connectionScope.launch {
                    if (transferId != null) TransferProgressManager.start(transferId, fragments.size)
                    var sent = 0
                    val awaiter = clientWriteAwaiter
                    fragments.forEach { frag ->
                        if (!isActive) return@launch
                        val fragData = frag.toBinaryData() ?: return@forEach
                        // Gate on write callback when path is client-write + awaiter is set.
                        // This matches the same flow-control used in broadcastPacket.
                        if (awaiter != null && clientConn != null) {
                            try { awaiter(clientConn.device.address) } catch (_: Exception) {}
                        }
                        sendDataToPeer(fragData, targetPeerID, gattServer, characteristic, isVideo)
                        // For video: awaitWritePermit already paces via write callback — no extra delay.
                        // For non-video: keep 20ms delay to preserve existing behaviour.
                        if (!isVideo) delay(20L)
                        if (transferId != null) {
                            sent++
                            TransferProgressManager.progress(transferId, sent, fragments.size)
                            if (sent == fragments.size) TransferProgressManager.complete(transferId, fragments.size)
                        }
                    }
                }
                return true
            }
        }

        // Single-fragment path (fits in one BLE packet)
        val data = packet.toBinaryData() ?: return false
        if (transferId != null) TransferProgressManager.start(transferId, 1)
        val ok = sendDataToPeer(data, targetPeerID, gattServer, characteristic, isVideo)
        if (ok && transferId != null) {
            TransferProgressManager.progress(transferId, 1, 1)
            TransferProgressManager.complete(transferId, 1)
        }
        return ok
    }

    /** Low-level: send raw bytes to a specific peer (server notification or client write). */
    private fun sendDataToPeer(
        data: ByteArray,
        targetPeerID: String,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?,
        noResponse: Boolean = false
    ): Boolean {
        // Server-side (we are the peripheral, peer subscribed via notification/indication)
        val subscribedList = connectionTracker.getSubscribedDevices()
        val connectedMap   = connectionTracker.getConnectedDevices()
        val serverTarget = subscribedList.firstOrNull { connectionTracker.addressPeerMap[it.address] == targetPeerID }
        // Find client-side connection that has a characteristic (service discovery completed).
        val clientTarget = connectedMap.values.firstOrNull {
            connectionTracker.addressPeerMap[it.device.address] == targetPeerID && it.characteristic != null
        }
        Log.d(TAG, "sendDataToPeer $targetPeerID: subs=${subscribedList.size} serverTarget=${serverTarget?.address} clientTarget=${clientTarget?.device?.address} clientChar=${clientTarget?.characteristic != null}")
        if (serverTarget != null && notifyDevice(serverTarget, data, gattServer, characteristic, confirm = !noResponse)) return true

        // Client-side (we are the central, write to peer's characteristic)
        if (clientTarget != null && writeToDeviceConn(clientTarget, data, noResponse)) return true

        Log.d(TAG, "sendDataToPeer $targetPeerID: no delivery path — dropping")
        return false
    }

    private fun sha256Hex(bytes: ByteArray): String = try {
        val md = java.security.MessageDigest.getInstance("SHA-256")
        md.update(bytes)
        md.digest().joinToString("") { "%02x".format(it) }
    } catch (_: Exception) { bytes.size.toString(16) }

    
    /**
     * Public entry point for broadcasting - submits request to actor for serialization
     */
    fun broadcastSinglePacket(
        routed: RoutedPacket,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?
    ) {
        if (routed.packet.type.toInt() == 17){
            debugManager?.measureRTT(0)
        }
        // Submit broadcast request to actor for serialized processing
        broadcasterScope.launch {
            try {
                broadcasterActor.send(BroadcastRequest(routed, gattServer, characteristic))
            } catch (e: Exception) {
                Log.w(TAG, "Failed to send broadcast request to actor: ${e.message}")
                // Fallback to direct processing if actor fails
                broadcastSinglePacketInternal(routed, gattServer, characteristic)
            }
        }
    }
    
    /**
     * Internal broadcast implementation - runs in serialized actor context
     */
    private suspend fun broadcastSinglePacketInternal(
        routed: RoutedPacket,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?
    ) {
        val packet = routed.packet
        val data = packet.toBinaryData() ?: return
        val typeName = MessageType.fromValue(packet.type)?.name ?: packet.type.toString()
        val senderPeerID = routed.peerID ?: packet.senderID.toHexString()
        val incomingAddr = routed.relayAddress
        val incomingPeer = incomingAddr?.let { connectionTracker.addressPeerMap[it] }
        val senderNick = senderPeerID.let { pid -> nicknameResolver?.invoke(pid) }
        
        if (packet.recipientID != SpecialRecipients.BROADCAST) {
            val recipientID = packet.recipientID?.let {
                String(it).replace("\u0000", "").trim()
            } ?: ""

            // Try to find the recipient in server connections (subscribedDevices)
            val targetDevice = connectionTracker.getSubscribedDevices()
                .firstOrNull { connectionTracker.addressPeerMap[it.address] == recipientID }
            
            // If found, send directly
            if (targetDevice != null) {
                Log.d(TAG, "Send packet type ${packet.type} directly to target device for recipient $recipientID: ${targetDevice.address}")
                if (notifyDevice(targetDevice, data, gattServer, characteristic)) {
                    val toPeer = connectionTracker.addressPeerMap[targetDevice.address]
                    logPacketRelay(typeName, senderPeerID, senderNick, incomingPeer, incomingAddr, toPeer, targetDevice.address, packet.ttl)
                    return  // Sent, no need to continue
                }
            }

            // Try to find the recipient in client connections (connectedDevices)
            val targetDeviceConn = connectionTracker.getConnectedDevices().values
                .firstOrNull { connectionTracker.addressPeerMap[it.device.address] == recipientID }
            
            // If found, send directly
            if (targetDeviceConn != null) {
                Log.d(TAG, "Send packet type ${packet.type} directly to target client connection for recipient $recipientID: ${targetDeviceConn.device.address}")
                if (writeToDeviceConn(targetDeviceConn, data)) {
                    val toPeer = connectionTracker.addressPeerMap[targetDeviceConn.device.address]
                    logPacketRelay(typeName, senderPeerID, senderNick, incomingPeer, incomingAddr, toPeer, targetDeviceConn.device.address, packet.ttl)
                    return  // Sent, no need to continue
                }
            }
        }

        // Else, continue with broadcasting to all devices
        val subscribedDevices = connectionTracker.getSubscribedDevices()
        val connectedDevices = connectionTracker.getConnectedDevices()

        Log.i(TAG, "Broadcasting packet type ${packet.type} to ${subscribedDevices.size} server + ${connectedDevices.size} client connections")
        val senderID = String(packet.senderID).replace("\u0000", "")
        
        // Send to server connections (devices connected to our GATT server)
        subscribedDevices.forEach { device ->
            if (device.address == routed.relayAddress) {
                Log.d(TAG, "Skipping broadcast to client back to relayer: ${device.address}")
                return@forEach
            }
            if (connectionTracker.addressPeerMap[device.address] == senderID) {
                Log.d(TAG, "Skipping broadcast to client back to sender: ${device.address}")
                return@forEach
            }
            val sent = notifyDevice(device, data, gattServer, characteristic)
            if (sent) {
                val toPeer = connectionTracker.addressPeerMap[device.address]
                logPacketRelay(typeName, senderPeerID, senderNick, incomingPeer, incomingAddr, toPeer, device.address, packet.ttl)
            }
        }
        
        // Send to client connections (GATT servers we are connected to)
        connectedDevices.values.forEach { deviceConn ->
            if (deviceConn.isClient && deviceConn.gatt != null && deviceConn.characteristic != null) {
                if (deviceConn.device.address == routed.relayAddress) {
                    Log.d(TAG, "Skipping broadcast to server back to relayer: ${deviceConn.device.address}")
                    return@forEach
                }
                if (connectionTracker.addressPeerMap[deviceConn.device.address] == senderID) {
                    Log.d(TAG, "Skipping roadcast to server back to sender: ${deviceConn.device.address}")
                    return@forEach
                }
                val sent = writeToDeviceConn(deviceConn, data)
                if (sent) {
                    val toPeer = connectionTracker.addressPeerMap[deviceConn.device.address]
                    logPacketRelay(typeName, senderPeerID, senderNick, incomingPeer, incomingAddr, toPeer, deviceConn.device.address, packet.ttl)
                }
            }
        }
    }
    
    /**
     * Send data to a single device (server->client).
     * [confirm] = true → indication (ACK required, reliable but slower).
     * [confirm] = false → notification (no ACK, higher throughput, use for video).
     */
    private fun notifyDevice(
        device: BluetoothDevice,
        data: ByteArray,
        gattServer: BluetoothGattServer?,
        characteristic: BluetoothGattCharacteristic?,
        confirm: Boolean = true
    ): Boolean {
        return try {
            characteristic?.let { char ->
                char.value = data
                val result = gattServer?.notifyCharacteristicChanged(device, char, confirm) ?: false
                result
            } ?: false
        } catch (e: Exception) {
            Log.w(TAG, "Error sending to server connection ${device.address}: ${e.message}")
            connectionScope.launch {
                delay(CLEANUP_DELAY)
                connectionTracker.removeSubscribedDevice(device)
                connectionTracker.addressPeerMap.remove(device.address)
            }
            false
        }
    }

    /**
     * Send data to a single device (client->server).
     * Uses WRITE_TYPE_DEFAULT when flow control is active (awaiter set) so
     * onCharacteristicWrite fires and releases the per-device semaphore.
     * Falls back to WRITE_TYPE_NO_RESPONSE only when no flow control is wired.
     */
    private fun writeToDeviceConn(
        deviceConn: BluetoothConnectionTracker.DeviceConnection,
        data: ByteArray,
        noResponse: Boolean = false
    ): Boolean {
        return try {
            deviceConn.characteristic?.let { char ->
                char.value = data
                // If clientWriteAwaiter is set, we MUST use WRITE_TYPE_DEFAULT so
                // onCharacteristicWrite fires and releases the per-device semaphore.
                // WRITE_TYPE_NO_RESPONSE never triggers the callback → deadlock.
                val useNoResponse = noResponse && clientWriteAwaiter == null
                char.writeType = if (useNoResponse)
                    BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                else
                    BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                val result = deviceConn.gatt?.writeCharacteristic(char) ?: false
                Log.d(TAG, "writeToDeviceConn ${deviceConn.device.address} noResponse=$useNoResponse size=${data.size} result=$result")
                result
            } ?: false
        } catch (e: Exception) {
            Log.w(TAG, "Error sending to client connection ${deviceConn.device.address}: ${e.message}")
            connectionScope.launch {
                delay(CLEANUP_DELAY)
                connectionTracker.cleanupDeviceConnection(deviceConn.device.address)
            }
            false
        }
    }
    
    /**
     * Get debug information
     */
    fun getDebugInfo(): String {
        return buildString {
            appendLine("=== Packet Broadcaster Debug Info ===")
            appendLine("Broadcaster Scope Active: ${broadcasterScope.isActive}")
            appendLine("Actor Channel Closed: ${broadcasterActor.isClosedForSend}")
            appendLine("Connection Scope Active: ${connectionScope.isActive}")
        }
    }
    
    /**
     * Shutdown the broadcaster actor gracefully
     */
    fun shutdown() {
        Log.d(TAG, "Shutting down BluetoothPacketBroadcaster actor")
        
        // Close the actor gracefully
        broadcasterActor.close()
        
        // Cancel the broadcaster scope
        broadcasterScope.cancel()
        
        Log.d(TAG, "BluetoothPacketBroadcaster shutdown complete")
    }
}

