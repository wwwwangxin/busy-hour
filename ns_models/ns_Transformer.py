from os import stat
import torch
import torch.nn as nn
from ns_layers.Transformer_EncDec import Decoder, DecoderLayer, Encoder, EncoderLayer
from ns_layers.SelfAttention_Family import DSAttention, AttentionLayer
from layers.Embed import DataEmbedding


class Projector(nn.Module):
    '''
    MLP to learn the De-stationary factors
    '''
    def __init__(self, enc_in, seq_len, hidden_dims, hidden_layers, output_dim, kernel_size=3):
        super(Projector, self).__init__()

        padding = 1 if torch.__version__ >= '1.5.0' else 2
        self.series_conv = nn.Conv1d(in_channels=seq_len, out_channels=1, kernel_size=kernel_size, padding=padding, padding_mode='circular', bias=False)

        layers = [nn.Linear(2 * enc_in, hidden_dims[0]), nn.ReLU()]
        for i in range(hidden_layers-1):
            layers += [nn.Linear(hidden_dims[i], hidden_dims[i+1]), nn.ReLU()]
        
        layers += [nn.Linear(hidden_dims[-1], output_dim, bias=False)]
        self.backbone = nn.Sequential(*layers)

    def forward(self, x, stats):
        # x:     B x S x E
        # stats: B x 1 x E
        # y:     B x O
        batch_size = x.shape[0]
        x = self.series_conv(x)          # B x 1 x E
        x = torch.cat([x, stats], dim=1) # B x 2 x E
        x = x.view(batch_size, -1) # B x 2E
        y = self.backbone(x)       # B x O

        return y

class ShiftingModule(nn.Module):
    '''
    MLP to learn the shifting of mean and standard variation
    '''
    def __init__(self, enc_in, seq_len, hidden_dims, hidden_layers, output_dim=2, kernel_size=3):
        super(ShiftingModule, self).__init__()

        padding = 1 if torch.__version__ >= '1.5.0' else 2
        #self.series_conv = nn.Conv1d(in_channels=seq_len, out_channels=1, kernel_size=kernel_size, padding=padding, padding_mode='circular', bias=False)

        layers = [nn.Linear(2 * enc_in, hidden_dims[0]), nn.ReLU()]
        for i in range(hidden_layers-1):
            layers += [nn.Linear(hidden_dims[i], hidden_dims[i+1]), nn.ReLU()]
        
        layers += [nn.Linear(hidden_dims[-1], output_dim, bias=False)]
        self.backbone = nn.Sequential(*layers)

    def forward(self, x, stats1,stats2):
        # x:     B x S x E
        # stats: B x 1 x E
        # y:     B x O
        batch_size = x.shape[0]
        #x = self.series_conv(x)          # B x 1 x E
        x = torch.cat([stats1, stats2], dim=1) # B x 2 x E
        x = x.view(batch_size, -1) # B x 3E
        y = self.backbone(x)       # B x 2 

        return y


class Model(nn.Module):
    """
    Non-stationary Transformer
    """
    def __init__(self, configs):
        super(Model, self).__init__()
        self.pred_len = configs.pred_len
        self.seq_len = configs.seq_len
        self.label_len = configs.label_len
        self.output_attention = configs.output_attention
        self.hour_day = configs.hour_day
        self.with_shift = configs.with_shift
        # Embedding
        self.enc_embedding = DataEmbedding(configs.enc_in, configs.d_model, configs.embed, configs.freq,
                                           configs.dropout)
        self.dec_embedding = DataEmbedding(configs.dec_in, configs.d_model, configs.embed, configs.freq,
                                           configs.dropout)
        # Encoder
        self.encoder = Encoder(
            [
                EncoderLayer(
                    AttentionLayer(
                        DSAttention(False, configs.factor, attention_dropout=configs.dropout,
                                      output_attention=configs.output_attention), configs.d_model, configs.n_heads),
                    configs.d_model,
                    configs.d_ff,
                    dropout=configs.dropout,
                    activation=configs.activation
                ) for l in range(configs.e_layers)
            ],
            norm_layer=torch.nn.LayerNorm(configs.d_model)
        )
        # Decoder
        self.decoder = Decoder(
            [
                DecoderLayer(
                    AttentionLayer(
                        DSAttention(True, configs.factor, attention_dropout=configs.dropout, output_attention=False),
                        configs.d_model, configs.n_heads),
                    AttentionLayer(
                        DSAttention(False, configs.factor, attention_dropout=configs.dropout, output_attention=False),
                        configs.d_model, configs.n_heads),
                    configs.d_model,
                    configs.d_ff,
                    dropout=configs.dropout,
                    activation=configs.activation,
                )
                for l in range(configs.d_layers)
            ],
            norm_layer=torch.nn.LayerNorm(configs.d_model),
            projection=nn.Linear(configs.d_model, configs.c_out, bias=True)
        )
        self.tau_learner   = Projector(enc_in=configs.enc_in, seq_len=self.seq_len, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=1)
        self.delta_learner = Projector(enc_in=configs.enc_in, seq_len=self.seq_len, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=self.seq_len)
        if self.hour_day=='h':
            if configs.with_shift == 'ws':
                self.shift_learner = ShiftingModule(enc_in=24, seq_len=self.seq_len//24, hidden_dims=[32,32], hidden_layers=2,output_dim=24*2)
            self.tau_learner_hour   = Projector(enc_in=24, seq_len=self.seq_len//24, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=24)
            self.delta_learner_hour = Projector(enc_in=24, seq_len=self.seq_len//24, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=24)
        else:
            self.tau_learner_day   = Projector(enc_in=self.seq_len//24, seq_len=24, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=self.seq_len//24)
            self.delta_learner_day = Projector(enc_in=self.seq_len//24, seq_len=24, hidden_dims=configs.p_hidden_dims, hidden_layers=configs.p_hidden_layers, output_dim=self.seq_len//24)


        if configs.busy_decoder:
            kernel_size = 24
            if configs.busy_decoder_modal == 'm':
                self.busy_decoder = torch.nn.MaxPool1d(kernel_size, stride=None, padding=0, dilation=1, return_indices=False, ceil_mode=False)
            else:
                self.busy_decoder = torch.nn.Linear(self.pred_len+self.label_len,(self.pred_len+self.label_len)//24)


    def forward(self, x_enc, x_mark_enc, x_dec, x_mark_dec,
                enc_self_mask=None, dec_self_mask=None, dec_enc_mask=None):

        x_raw = x_enc.clone().detach()
        
        # Normalization
        
        if self.hour_day == 'h':
            x_enc_hour_day = x_enc.reshape(x_enc.shape[0],-1,24)
            mean_enc_hour = x_enc_hour_day.mean(1, keepdim=True).detach() # B x 1 x 24
            #mean_enc_day = x_enc_hour_day.mean(2, keepdim=True).detach() # B x 1 x day
            std_enc_hour = torch.sqrt(torch.var(x_enc_hour_day, dim=1, keepdim=True, unbiased=False) + 1e-5).detach() # B x 1 x 24
            #std_enc_day = torch.sqrt(torch.var(x_enc_hour_day, dim=2, keepdim=True, unbiased=False) + 1e-5).detach() # B x 1 x day
            x_enc_hour_day = (x_enc_hour_day-mean_enc_hour)/std_enc_hour
            x_enc = x_enc_hour_day.reshape(x_enc.shape[0],-1,1)
            tau_hour = self.tau_learner_hour(x_enc_hour_day,std_enc_hour).exp()
            delta_hour = self.delta_learner_hour(x_enc_hour_day,mean_enc_hour)
            if self.with_shift == 'ws':
                shift_hour = self.shift_learner(x_enc_hour_day,mean_enc_hour,std_enc_hour).unsqueeze(1)
            tau = None
            delta = None
            tau_day = None
            delta_day = None
        elif self.hour_day == 'd':
            x_enc_hour_day = x_enc.reshape(x_enc.shape[0],-1,24)
            mean_enc_day = x_enc_hour_day.mean(2, keepdim=True).detach() # B x 1 x day
            std_enc_day = torch.sqrt(torch.var(x_enc_hour_day, dim=2, keepdim=True, unbiased=False) + 1e-5).detach() # B x 1 x day
            x_enc_hour_day = (x_enc_hour_day-mean_enc_day)/std_enc_day
            x_enc = x_enc_hour_day.reshape(x_enc.shape[0],-1,1)
            tau_day = self.tau_learner_day(x_enc_hour_day.permute(0,2,1),std_enc_day.permute(0,2,1)).exp()
            delta_day = self.delta_learner_day(x_enc_hour_day.permute(0,2,1),mean_enc_day.permute(0,2,1))
            tau = None
            delta = None
            tau_hour = None
            delta_hour = None
        else:
            tau_hour = None
            delta_hour = None
            tau_day = None
            delta_day = None
            mean_enc = x_enc.mean(1, keepdim=True).detach() # B x 1 x E
            x_enc = x_enc - mean_enc
            std_enc = torch.sqrt(torch.var(x_enc, dim=1, keepdim=True, unbiased=False) + 1e-5).detach() # B x 1 x E
            x_enc = x_enc / std_enc
            tau = self.tau_learner(x_raw, std_enc).exp()     # B x S x E, B x 1 x E -> B x 1, positive scalar    
            delta = self.delta_learner(x_raw, mean_enc) 
        x_dec_new = torch.cat([x_enc[:, -self.label_len: , :], torch.zeros_like(x_dec[:, -self.pred_len:, :])], dim=1).to(x_enc.device).clone()
        #tau = self.tau_learner(x_raw, std_enc).exp()     # B x S x E, B x 1 x E -> B x 1, positive scalar    
        #delta = self.delta_learner(x_raw, mean_enc)      # B x S x E, B x 1 x E -> B x S
        

        
        # Model Inference
        enc_out = self.enc_embedding(x_enc, x_mark_enc)
        enc_out, attns = self.encoder(enc_out, attn_mask=enc_self_mask, tau=tau, delta=delta, tau_hour=tau_hour,delta_hour=delta_hour,tau_day=tau_day,delta_day=delta_day)

        dec_out = self.dec_embedding(x_dec_new, x_mark_dec)
        dec_out = self.decoder(dec_out, enc_out, x_mask=dec_self_mask, cross_mask=dec_enc_mask, tau=tau, delta=delta,tau_hour=tau_hour,delta_hour=delta_hour,tau_day=tau_day,delta_day=delta_day)

        # De-normalization
        if self.hour_day=='h':
            if self.with_shift != 'ws':
                std_enc_hour=std_enc_hour.permute(0,2,1).repeat(1,dec_out.shape[1]//24,1)
                mean_enc_hour=mean_enc_hour.permute(0,2,1).repeat(1,dec_out.shape[1]//24,1)
            else:
                std_enc_hour = shift_hour[:,:,:24].exp().permute(0,2,1).repeat(1,dec_out.shape[1]//24,1)
                mean_enc_hour = shift_hour[:,:,24:].permute(0,2,1).repeat(1,dec_out.shape[1]//24,1)
            dec_out = dec_out * std_enc_hour + mean_enc_hour
        elif self.hour_day=='d':
            std_enc_day=std_enc_day.repeat(1,1,24).reshape(std_enc_day.shape[0],1,-1)
            mean_enc_day=mean_enc_day.repeat(1,1,24).reshape(std_enc_day.shape[0],1,-1)
            dec_out = dec_out * std_enc_day + mean_enc_day
        else:
            dec_out = dec_out * std_enc + mean_enc
        if self.busy_decoder:
            busy_out = self.busy_decoder(dec_out[:,:,0])
            return dec_out[:, -self.pred_len:, :], busy_out[:,-self.pred_len//24:]
        if self.output_attention:
            return dec_out[:, -self.pred_len:, :], attns
        else:
            return dec_out[:, -self.pred_len:, :]  # [B, L, D]
