require 'rails_helper'

RSpec.describe 'Api::V1::Bookings', type: :request do
  describe 'GET /api/v1/bookings' do
    it 'returns a list of bookings' do
      create_list(:booking, 3)

      get '/api/v1/bookings'

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json.length).to eq(3)
    end
  end

  describe 'POST /api/v1/bookings' do
    it 'creates a booking with valid params' do
      client = create(:client)
      freelancer = create(:freelancer)

      params = {
        booking: {
          client_id: client.id,
          freelancer_id: freelancer.id,
          start_date: Date.tomorrow,
          end_date: 1.week.from_now.to_date,
          total_amount: 500.00
        }
      }

      post '/api/v1/bookings', params: params

      expect(response).to have_http_status(:created)
      json = JSON.parse(response.body)
      expect(json['status']).to eq('pending')
    end

    it 'returns errors with invalid params' do
      params = { booking: { client_id: nil, freelancer_id: nil } }

      post '/api/v1/bookings', params: params

      expect(response).to have_http_status(:unprocessable_content)
    end
  end

  describe 'PATCH /api/v1/bookings/:id/confirm' do
    it 'confirms a pending booking' do
      booking = create(:booking, status: :pending)

      patch "/api/v1/bookings/#{booking.id}/confirm"

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['status']).to eq('confirmed')
    end

    it 'rejects confirming an already completed booking' do
      booking = create(:booking, status: :completed)

      patch "/api/v1/bookings/#{booking.id}/confirm"

      expect(response).to have_http_status(:unprocessable_content)
    end
  end

  describe 'PATCH /api/v1/bookings/:id/complete' do
    it 'completes a confirmed booking' do
      booking = create(:booking, status: :confirmed)

      patch "/api/v1/bookings/#{booking.id}/complete"

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['status']).to eq('completed')
    end
  end

  describe 'PATCH /api/v1/bookings/:id/cancel' do
    it 'cancels a pending booking' do
      booking = create(:booking, status: :pending)

      patch "/api/v1/bookings/#{booking.id}/cancel"

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['status']).to eq('cancelled')
    end
  end
end
